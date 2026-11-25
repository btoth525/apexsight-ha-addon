"""Push-log races and bridge ordering (1.31.1).

  * A claim is taken BEFORE the APNs send. A relay that dies between the two (an add-on restart or
    update mid-alert) left the claim behind, and every retry of that alert read "already sent" — a
    missed alert. A claim still 'sending' from another boot is now taken over.
  * A request that finds the phase in flight in ANOTHER request must not report success: that send
    can still fail, and its caller may be gone (the bridge's 10 s POST timeout). It now asks for a
    retry (503), and the retry either finds it sent or delivers it.
  * The 1.31.0 push_log table (no state column) is migrated; its rows read as delivered.
  * Bridge: a description waits (bounded) for its review's alert to confirm, so the plain alert
    can't land second and overwrite the AI sentence; the forward runs off paho's thread.
  * Bridge: on_message never raises (paho re-raises callback exceptions out of loop_forever, which
    costs a 10 s reconnect during which every alert is lost).

APNs is mocked. Run:  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_push_log_races.py
"""
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import time

os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_races_")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
os.environ["APEX_SECRET_KEY"] = "testsecret"

# A 1.31.0 database: push_log without the state column, with one delivered row in it.
from app import config  # noqa: E402
with sqlite3.connect(config.DB_PATH) as conn:
    conn.execute("CREATE TABLE push_log (review_id TEXT NOT NULL, device_token TEXT NOT NULL, "
                 "phase TEXT NOT NULL, content_hash TEXT NOT NULL, ts REAL NOT NULL, "
                 "PRIMARY KEY (review_id, device_token, phase))")
    conn.execute("INSERT INTO push_log VALUES ('old-review', ?, 'alert', 'h', ?)", ("a" * 64, time.time()))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
importlib.reload(m)
from app import apns, db  # noqa: E402
import bridge  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


CODE = os.environ["PAIRING_CODE"]
PHONE = "a" * 64
sent = []


async def fake_send(token, environment, payload, collapse_id=""):
    sent.append({"token": token, "payload": payload, "collapse": collapse_id})
    return True, "ok"


async def no_la(*args):
    return None

apns.send_to_token = fake_send
m.apns.is_configured = lambda: True
m.liveactivity.on_person_alert = no_la


def alert(review):
    return {"pairing_code": CODE, "camera": "doorbell", "review_id": review, "collapse_id": review,
            "title": "Person", "body": "Doorbell", "stage": "alert", "labels": ["person"]}


with TestClient(m.app) as c:
    db.upsert_device(PHONE, m._canonical_code(CODE), "production", "ios")
    db.set_config(f"dedup_window:{CODE}", "0")

    # ---- migration ----
    rows = db.pushes_for("old-review", PHONE)
    check("1.31.0 push_log migrated in place (row kept)", len(rows) == 1)
    check("…and a pre-migration row reads as delivered", db.claim_push("old-review", PHONE, "alert", "h") == "sent")
    sent.clear()
    c.post("/v1/notify", json=alert("old-review"))
    check("…so its alert is not re-sent after the upgrade", not sent)

    # ---- a claim orphaned by a relay that died mid-send ----
    r1 = "1795700000.000000-crash1"
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("INSERT INTO push_log(review_id, device_token, phase, content_hash, ts, state) "
                     "VALUES(?,?,?,?,?,?)", (r1, PHONE, "alert", "x", time.time(), "sending:deadbeef0000"))
    sent.clear()
    r = c.post("/v1/notify", json=alert(r1))
    check("a claim left 'sending' by a previous boot is taken over: the alert goes out",
          r.status_code == 200 and len(sent) == 1)
    check("…and is then recorded as delivered", db.claim_push(r1, PHONE, "alert", "x") == "sent")

    # ---- a phase in flight in another request ----
    r2 = "1795700100.000000-busy01"
    check("a fresh claim by this process", db.claim_push(r2, PHONE, "alert", "x") == "claimed")
    sent.clear()
    r = c.post("/v1/notify", json=alert(r2))
    check("a request that finds the alert in flight elsewhere sends nothing…", not sent)
    check("…and asks the bridge to retry instead of reporting success", r.status_code == 503)
    db.release_push(r2, PHONE, "alert")          # the in-flight send failed
    r = c.post("/v1/notify", json=alert(r2))
    check("after the other send failed, the retry delivers the alert", r.status_code == 200 and len(sent) == 1)
    r = c.post("/v1/notify", json=alert(r2))
    check("once delivered, a further retry is a plain 'already sent' (200, nothing sent)",
          r.status_code == 200 and len(sent) == 1)
    check("release never deletes a delivered row", (db.release_push(r2, PHONE, "alert") or True)
          and db.claim_push(r2, PHONE, "alert", "x") == "sent")

    # ---- a claim stuck in THIS process far past any send is reclaimable ----
    r3 = "1795700200.000000-stuck1"
    db.claim_push(r3, PHONE, "alert", "x")
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("UPDATE push_log SET ts = ? WHERE review_id = ?", (time.time() - db.SENDING_STALE_S - 5, r3))
    check("a claim stuck past SENDING_STALE_S is taken over", db.claim_push(r3, PHONE, "alert", "x") == "claimed")

    # ---- the summary scheduler: a failing summary no longer skips the prune ----
    pruned = []
    real_recap, real_prune = m._maybe_send_recap, db.prune_push_log

    async def broken_recap():
        raise RuntimeError("bad stored config")

    m._maybe_send_recap = broken_recap
    db.prune_push_log = lambda ts: pruned.append(ts)
    import asyncio
    tick = asyncio.run(m._scheduler_pass(0))
    check("scheduler: a raising summary still runs the hourly push_log prune", tick == 1 and len(pruned) == 1)
    m._maybe_send_recap, db.prune_push_log = real_recap, real_prune

# ---- bridge: description waits for its review's alert ----
posted = []
order = []
bridge._post_to_relay = lambda payload, stage, attempts=3: posted.append(stage) or order.append(("post", time.time())) or True
REV = "1795700300.000000-descw1"
rec = {"review_id": REV, "title": "Person", "camera": "doorbell", "apex_url": "", "labels": ["person"],
       "zones": [], "_t": time.time()}
bridge._pending["det-1"] = rec
bridge._described.clear()
bridge._notified.pop(REV, None)
threads = []
bridge._run_in_background = lambda fn, *a: threads.append(threading.Thread(target=fn, args=a)) or threads[-1].start()
t0 = time.time()
bridge._handle_description({"type": "description", "id": "det-1", "description": "A courier at the door."})
check("bridge: _handle_description returns at once (the forward is off paho's thread)", time.time() - t0 < 0.3)
time.sleep(0.8)
check("…the description is held while its alert is unconfirmed", not posted)
stamped_at = time.time()
bridge._notified[REV] = {"alert": stamped_at}
for t in threads:
    t.join(5)
check("…and goes out once the alert is confirmed", posted == ["description"] and order[0][1] >= stamped_at)

posted.clear()
bridge._DESC_WAIT_FOR_ALERT_S = 0.6
REV2 = "1795700400.000000-descw2"
bridge._pending["det-2"] = dict(rec, review_id=REV2)
threads.clear()
bridge._handle_description({"type": "description", "id": "det-2", "description": "Someone at the door."})
for t in threads:
    t.join(5)
check("…but never waits forever: an alert that never confirms still lets the description out", posted == ["description"])

# ---- bridge: on_message is exception-safe ----


class Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload.encode()


bad = [(bridge.EVENTS_TOPIC, "null"), (bridge.EVENTS_TOPIC, "[1, 2]"),
       (bridge.EVENTS_TOPIC, json.dumps({"type": "new", "after": "oops"})),
       (bridge.TOPIC, json.dumps({"type": "new", "after": ["x"]})), (bridge.TOPIC, "7"),
       (bridge.DESC_TOPIC, json.dumps({"type": "description", "id": ["x"], "description": "d"})),
       (bridge.DESC_TOPIC, "\"just a string\"")]
raised = []
for topic, payload in bad:
    try:
        bridge.on_message(None, None, Msg(topic, payload))
    except Exception as exc:  # noqa: BLE001
        raised.append((topic, payload, exc))
check("bridge: malformed MQTT messages never escape on_message (no reconnect pause)", not raised)

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
