"""Smart-alert delivery through /v1/notify and the GET /v1/vehicles endpoint (1.31.0).

A `kind` push arrives finished from the bridge, so the relay must NOT re-render it, must neither read
nor write the duplicate-review state, must not start a Live Activity — and must still ride every gate
(the synthetic label is what the app's per-object toggle mutes). APNs is mocked.

Run:  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_notify_kind.py
"""
import importlib
import json
import os
import tempfile

os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_kind_")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
os.environ["APEX_SECRET_KEY"] = "testsecret"

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
importlib.reload(m)
from app import apns, db  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


CODE = os.environ["PAIRING_CODE"]
DEV = "a" * 64
sent = []
la_calls = []


async def fake_send(token, environment, payload, collapse_id=""):
    sent.append((token, payload, collapse_id))
    return True, "ok"


async def fake_la(*args):
    la_calls.append(args)

apns.send_to_token = fake_send
m.apns.is_configured = lambda: True
m.liveactivity.on_person_alert = fake_la

# ---- payload builder ----
p = apns.build_payload(title="t", body="b", review_id="r", camera="doorbell", time_sensitive=True,
                       detection_id="e1", kind="loitering")
check("time_sensitive → time-sensitive interruption level", p["aps"].get("interruption-level") == "time-sensitive")
check("time_sensitive is audible", p["aps"].get("sound") == "default")
check("detection_id reaches the app (long-press follow)", p.get("detection_id") == "e1")
check("kind reaches the app", p.get("kind") == "loitering")
p = apns.build_payload(title="t", body="b", review_id="r", camera="doorbell")
check("a plain alert is unchanged (no level, no kind)", "interruption-level" not in p["aps"] and "kind" not in p)

with TestClient(m.app) as c:
    db.upsert_device(DEV, m._canonical_code(CODE), "production", "ios")
    # A real person review was just pushed on this camera: the dedup state a smart push must not touch.
    db.set_config(f"recentnotif:{CODE}:doorbell",
                  json.dumps({"ts": __import__("time").time(), "review_id": "rev-person",
                              "labels": ["loitering", "person"], "suppressed": []}))
    before = db.get_config(f"recentnotif:{CODE}:doorbell")
    body = {"pairing_code": CODE, "title": "\U0001f9cd Someone has been at the front door for 2 min",
            "body": "Doorbell · since 4:10 PM · still there", "camera": "doorbell",
            "review_id": "rev-person", "collapse_id": "linger-e1", "detection_id": "e1",
            "labels": ["loitering", "person"], "zones": ["side_walk"], "kind": "loitering",
            "time_sensitive": True, "snapshot_url": "https://nvr.example/api/events/e1/snapshot.jpg"}
    r = c.post("/v1/notify", json=body)
    check("smart push delivered", r.status_code == 200 and len(sent) == 1)
    payload = sent[0][1] if sent else {}
    check("…words untouched by the style renderer", payload.get("aps", {}).get("alert", {}).get("title") == body["title"]
          and payload["aps"]["alert"]["body"] == body["body"])
    check("…not dropped as a duplicate of the person review (same labels, same review, in the window)", len(sent) == 1)
    check("…dedup state neither read-suppressed nor re-stamped",
          db.get_config(f"recentnotif:{CODE}:doorbell") == before)
    check("…no Live Activity for a smart push, even with 'person' in labels", not la_calls)
    check("…time-sensitive", payload.get("aps", {}).get("interruption-level") == "time-sensitive")
    check("…its own collapse id", sent and sent[0][2] == "linger-e1")
    check("…label for the foreground re-check is the synthetic one", payload.get("label") == "loitering")

    # Per-device gate: the app's "Lingering" toggle writes objects_disabled ["loitering"].
    db.set_config(f"prefs:{DEV}", json.dumps({"objects_disabled": ["loitering"]}))
    sent.clear()
    r = c.post("/v1/notify", json=dict(body, review_id="rev-e2", collapse_id="linger-e2", detection_id="e2"))
    check("the app's per-kind toggle (objects_disabled) mutes it", r.status_code == 200 and not sent)
    db.set_config(f"prefs:{DEV}", json.dumps({"objects_disabled": ["person"]}))
    sent.clear()
    c.post("/v1/notify", json=dict(body, review_id="rev-e3", collapse_id="linger-e3"))
    check("muting plain 'person' does NOT mute the lingering alert", len(sent) == 1)
    db.set_config(f"prefs:{DEV}", json.dumps({"cameras_disabled": ["doorbell"]}))
    sent.clear()
    c.post("/v1/notify", json=dict(body, review_id="rev-e4", collapse_id="linger-e4"))
    check("a muted camera still applies", not sent)
    db.set_config(f"prefs:{DEV}", "{}")
    db.set_config(f"gate:{CODE}", json.dumps({"disarmed": True}))
    sent.clear()
    c.post("/v1/notify", json=dict(body, review_id="rev-e5", collapse_id="linger-e5"))
    check("household disarm still applies", not sent)
    db.set_config(f"gate:{CODE}", json.dumps({"disarmed": False}))

    sent.clear()
    c.post("/v1/notify", json=dict(body, review_id="rev-e3", collapse_id="linger-e3"))
    check("the same smart alert (same collapse id) never goes out twice", not sent)

    # 1.31.1: several smart alerts can share one Frigate review — two cars that pull in together are
    # one review, and a parked car's arrival and its departure hours later are one long event that
    # /api/review/event/<id> maps to the same review. Each is its own alert.
    veh = dict(body, kind="vehicle_presence", labels=["vehicle_presence"], time_sensitive=False,
               camera="Front_Driveway", zones=["driveway"], review_id="rev-two-cars")
    sent.clear()
    c.post("/v1/notify", json=dict(veh, title="Family Truck arrived home", collapse_id="veh-Family_Truck-1800000000"))
    c.post("/v1/notify", json=dict(veh, title="Family Jeep arrived home", collapse_id="veh-Family_Jeep-1800000004"))
    check("two vehicles arriving in ONE review → two alerts (the second is not 'already sent')",
          [p["aps"]["alert"]["title"] for _, p, _ in sent] == ["Family Truck arrived home", "Family Jeep arrived home"])
    sent.clear()
    c.post("/v1/notify", json=dict(veh, title="Family Truck left", collapse_id="veh-Family_Truck-1800030000"))
    check("…and the same truck leaving later on the same long event's review is delivered too", len(sent) == 1)

    # A smart alert with NO review (a pickup nobody was reviewed for) is logged by its collapse id, so a
    # retry after one phone's transient failure reaches only that phone.
    DEV_B = "b" * 64
    db.upsert_device(DEV_B, m._canonical_code(CODE), "production", "ios")
    real_send = apns.send_to_token
    fail = {DEV_B}

    async def flaky_send(token, environment, payload, collapse_id=""):
        if token in fail:
            return False, "503 ServiceUnavailable"
        return await real_send(token, environment, payload, collapse_id)

    apns.send_to_token = flaky_send
    pickup = dict(body, kind="package_pickup", labels=["package_pickup"], review_id="",
                  collapse_id="pickup-1800000100.0-abc", title="Someone took the package")
    sent.clear()
    r = c.post("/v1/notify", json=pickup)
    check("no-review smart alert, one phone failing → retry requested", r.status_code == 503 and len(sent) == 1)
    fail.clear()
    sent.clear()
    r = c.post("/v1/notify", json=pickup)
    check("…the retry reaches ONLY the phone that missed it (no second time-sensitive buzz)",
          r.status_code == 200 and [t for t, _, _ in sent] == [DEV_B])
    apns.send_to_token = real_send

    # ---- GET /v1/vehicles ----
    r = c.get("/v1/vehicles", params={"pairing_code": "APEX-WRNG-0000"})
    check("vehicles: wrong pairing code → 403", r.status_code == 403)
    r = c.get("/v1/vehicles", params={"pairing_code": CODE})
    check("vehicles: nothing stored yet → empty list", r.status_code == 200 and r.json()["vehicles"] == [])
    db.set_config(f"vehicles:{CODE}", json.dumps({
        "updated": 1800000000.0, "known": ["Sams_Truck", "Jos_Jeep"],
        "vehicles": {"Sams_Truck": {"name": "Sam's Truck", "status": "home", "since": 1799990000.0,
                                    "changed": 1799990005.0, "camera": "Front_Driveway", "event_id": "x",
                                    "spots": {"Front_Driveway": [0.4, 0.0, 0.1, 0.3]}},
                     "Jos_Jeep": {"name": "Jo's Jeep", "status": "unknown", "since": 0, "changed": 0}}}))
    r = c.get("/v1/vehicles", params={"pairing_code": CODE})
    v = {x["id"]: x for x in r.json()["vehicles"]}
    check("vehicles: status + since + last change", v["Sams_Truck"]["status"] == "home"
          and v["Sams_Truck"]["since"] == 1799990000.0 and v["Sams_Truck"]["last_change"] == 1799990005.0)
    check("vehicles: display name", v["Sams_Truck"]["name"] == "Sam's Truck")
    check("vehicles: never-seen vehicle reads unknown with no time", v["Jos_Jeep"]["status"] == "unknown"
          and v["Jos_Jeep"]["since"] is None)
    check("vehicles: internals (spots, event ids) stay server-side",
          "spots" not in v["Sams_Truck"] and "event_id" not in v["Sams_Truck"])
    db.set_config(f"vehicles:{CODE}", "not json")
    r = c.get("/v1/vehicles", params={"pairing_code": CODE})
    check("vehicles: a corrupt blob degrades to empty, never 500", r.status_code == 200 and r.json()["vehicles"] == [])

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
