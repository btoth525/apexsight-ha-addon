"""One activity, one alert (1.31.0) — the build-291 duplicate-notification bug.

A driveway review with three objects (two dogs and a person) reached the phone as FOUR alerting
banners: the bridge forwarded [alert], then [description] twice (one per described object), then
[final], and each went out as its own alerting push. Now:
  * the bridge sends ONE description per review, however many objects Frigate describes;
  * every follow-up (description, final) replaces the alert in place: same apns-collapse-id (the
    review id, as the alert uses), interruption-level passive, no sound, no badge — even when the AI
    rated it above routine;
  * the relay logs every (review, phone, phase) it delivers, persistently: the same phase never goes
    out twice (a bridge retry, a second forward, a restart), and a follow-up identical to what the
    phone already shows isn't sent at all;
  * a failed send releases its claim, so the retry reaches only the phone that missed it.

APNs is mocked. Run:  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_one_alert_per_activity.py
"""
import importlib
import os
import tempfile

os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_once_")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
os.environ["APEX_SECRET_KEY"] = "testsecret"

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
PHONE_A, PHONE_B = "a" * 64, "b" * 64
REVIEW = "1795615490.016863-mhc1ze"
sent = []
fail_tokens = set()


async def fake_send(token, environment, payload, collapse_id=""):
    if token in fail_tokens:
        return False, "503 ServiceUnavailable"
    sent.append({"token": token, "payload": payload, "collapse": collapse_id})
    return True, "ok"


async def no_la(*args):
    return None

apns.send_to_token = fake_send
m.apns.is_configured = lambda: True
m.liveactivity.on_person_alert = no_la


def audible(p):
    aps = p["payload"]["aps"]
    return aps.get("interruption-level") != "passive"


# The bridge's own payloads for the review, as it forwarded them.
base = {"pairing_code": CODE, "camera": "Front_Driveway", "review_id": REVIEW, "collapse_id": REVIEW,
        "apex_url": f"apex://review?id={REVIEW}", "camera_name": "Front Driveway",
        "labels": ["dog", "person"], "zones": ["driveway"], "severity": "alert",
        "detection_id": "1795615489.548644-ee4hld", "frigate_base_url": "https://nvr.example"}
alert = dict(base, title="\U0001f415 Dog, Person", body="Front Driveway", stage="alert", silent=False)
desc1 = {"pairing_code": CODE, "title": "\U0001f415 Dog, Person", "camera": "Front_Driveway",
         "body": "The dog is medium-sized with a brown and black coat.", "review_id": REVIEW,
         "collapse_id": REVIEW, "is_description": True, "announce": True, "labels": ["dog", "person"],
         "zones": ["driveway"]}
desc2 = dict(desc1, body="The dog is medium-sized with a dark brown coat and on a leash.")
final = dict(base, title="\U0001f7e1 Dog loose in the driveway", body="A loose dog wanders the driveway.",
             stage="final", silent=False, ai_title="Dog loose in the driveway",
             ai_summary="A loose dog wanders the driveway.", threat_level=1,
             snapshot_url="https://nvr.example/api/Front_Driveway/start/1/end/2/preview.gif",
             thumbnail_url="https://nvr.example/api/events/x/snapshot.jpg")

with TestClient(m.app) as c:
    db.upsert_device(PHONE_A, m._canonical_code(CODE), "production", "ios")

    # ---- the exact build-291 sequence ----
    for body in (alert, desc1, desc2, final):
        r = c.post("/v1/notify", json=body)
        check(f"relay accepts {body.get('stage') or 'description'}", r.status_code == 200)
    a = [p for p in sent if p["token"] == PHONE_A]
    check("alert → description → description → final reaches the phone as THREE pushes (2nd description dropped)",
          len(a) == 3)
    check("exactly ONE audible push", sum(1 for p in a if audible(p)) == 1 and audible(a[0]))
    check("the audible one is the alert, with sound", a[0]["payload"]["aps"].get("sound") == "default")
    check("every follow-up is passive with no sound",
          all(p["payload"]["aps"].get("interruption-level") == "passive" and "sound" not in p["payload"]["aps"]
              for p in a[1:]))
    check("…even the AI-escalated final (threat_level 1)", a[-1]["payload"].get("threat_level") == 1
          and a[-1]["payload"]["aps"].get("interruption-level") == "passive")
    check("every push shares the alert's collapse id (the review id) → replaced in place",
          {p["collapse"] for p in a} == {REVIEW})
    check("follow-ups never bump the badge", all(p["payload"].get("no_badge") for p in a[1:]))
    check("the replacement carries the better words",
          a[-1]["payload"]["aps"]["alert"]["title"] == final["title"])

    # ---- a bridge retry / re-forward of the same phase ----
    sent.clear()
    c.post("/v1/notify", json=final)
    c.post("/v1/notify", json=desc1)
    c.post("/v1/notify", json=alert)
    check("re-POSTing alert, description or final for the same review sends nothing", not sent)
    check("persisted: the log survives in the DB", len(db.pushes_for(REVIEW, PHONE_A)) == 3)

    # ---- an unchanged follow-up is not sent ----
    r2 = "1795620000.000000-abcdef"
    sent.clear()
    c.post("/v1/notify", json=dict(alert, review_id=r2, collapse_id=r2, title="T", body="B",
                                   detection_id="", labels=[], snapshot_url="", thumbnail_url=""))
    c.post("/v1/notify", json=dict(desc1, review_id=r2, collapse_id=r2, title="T", body="B", labels=[]))
    check("a follow-up identical to what the phone shows is skipped", len(sent) == 1)

    # The later sub-tests post several reviews with the same camera and labels back to back; the
    # household duplicate-review window (a separate, older guard) would rightly swallow them.
    db.set_config(f"dedup_window:{CODE}", "0")

    # ---- the collapse id is the review id even if a follow-up forgot it ----
    r3 = "1795630000.000000-zzzzzz"
    sent.clear()
    c.post("/v1/notify", json=dict(alert, review_id=r3, collapse_id=r3))
    c.post("/v1/notify", json=dict(desc1, review_id=r3, collapse_id=""))
    check("a follow-up without a collapse id still replaces the alert", [p["collapse"] for p in sent] == [r3, r3])

    # ---- a failed send: only the phone that missed it gets the retry ----
    db.upsert_device(PHONE_B, m._canonical_code(CODE), "production", "ios")
    r4 = "1795640000.000000-yyyyyy"
    sent.clear()
    fail_tokens.add(PHONE_B)
    r = c.post("/v1/notify", json=dict(alert, review_id=r4, collapse_id=r4))
    check("one phone failing transiently asks the bridge to retry", r.status_code == 503)
    fail_tokens.clear()
    sent.clear()
    c.post("/v1/notify", json=dict(alert, review_id=r4, collapse_id=r4))
    check("the retry reaches only the phone that missed it (no re-buzz for the other)",
          [p["token"] for p in sent] == [PHONE_B] and audible(sent[0]))

    # ---- a phone whose alert never landed: the follow-up is NOT its first audible alert ----
    r5 = "1795650000.000000-xxxxxx"
    db.set_config(f"prefs:{PHONE_B}", '{"quiet_hours": {"enabled": false}}')
    fail_tokens.add(PHONE_B)
    c.post("/v1/notify", json=dict(alert, review_id=r5, collapse_id=r5))
    fail_tokens.clear()
    sent.clear()
    c.post("/v1/notify", json=dict(final, review_id=r5, collapse_id=r5))
    b = [p for p in sent if p["token"] == PHONE_B]
    check("a follow-up to a phone that never got the alert is passive, not a first buzz",
          len(b) == 1 and not audible(b[0]))

    # ---- a send that RAISES must not leave a claim that swallows the retry ----
    r6 = "1795660000.000000-wwwwww"
    real_deliver = m.apns.deliver_to_pairing

    async def exploding(code, payload, collapse_id="", gate=None):
        for row in db.devices_for(code):
            gate(row["device_token"])          # claims are taken…
        raise RuntimeError("APNs credentials vanished mid-send")   # …then the send blows up

    m.apns.deliver_to_pairing = exploding
    try:
        c.post("/v1/notify", json=dict(alert, review_id=r6, collapse_id=r6))
    except Exception:
        pass
    m.apns.deliver_to_pairing = real_deliver
    sent.clear()
    c.post("/v1/notify", json=dict(alert, review_id=r6, collapse_id=r6))
    check("after a send that raised, the retry still reaches every phone", len(sent) == 2)

    # ---- the household duplicate window: a suppressed alert's follow-ups are dropped too ----
    db.set_config(f"dedup_window:{CODE}", "300")
    rA, rB = "1795670000.000000-aaaaaa", "1795670100.000000-bbbbbb"
    sent.clear()
    cam = {"camera": "Side_Gate"}   # a camera with no earlier alert in this test's window
    c.post("/v1/notify", json=dict(alert, review_id=rA, collapse_id=rA, **cam))
    c.post("/v1/notify", json=dict(alert, review_id=rB, collapse_id=rB, **cam))
    n_alerts = len(sent)
    c.post("/v1/notify", json=dict(desc1, review_id=rB, collapse_id=rB, **cam))
    c.post("/v1/notify", json=dict(final, review_id=rB, collapse_id=rB, **cam))
    check("a duplicate review's alert is suppressed", n_alerts == 2)   # rA to both phones only
    check("…and so are its description AND its AI-escalated final (no orphan notifications)",
          len(sent) == n_alerts)

# ---- the bridge: one description per review, however many objects are described ----
posted = []
bridge._post_to_relay = lambda payload, stage, attempts=3: posted.append((stage, payload)) or True
bridge._pending.clear()
bridge._described.clear()
record = {"review_id": REVIEW, "title": "Dog, Person", "camera": "Front_Driveway", "apex_url": "",
          "labels": ["dog", "person"], "zones": ["driveway"], "_t": __import__("time").time()}
for det in ("1795615516.504174-l27xmo", "1795615496.580562-b3jnjq", "1795615489.548644-ee4hld"):
    bridge._pending[det] = record
for det, text in (("1795615516.504174-l27xmo", "The dog is brown and black."),
                  ("1795615496.580562-b3jnjq", "The dog is on a leash."),
                  ("1795615489.548644-ee4hld", "The person wears a dark jacket.")):
    bridge._handle_description({"type": "description", "id": det, "description": text})
check("bridge: three described objects in one review → ONE description forward",
      [s for s, _ in posted] == ["description"])
check("…and it replaces the review's alert", posted and posted[0][1]["collapse_id"] == REVIEW)

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
