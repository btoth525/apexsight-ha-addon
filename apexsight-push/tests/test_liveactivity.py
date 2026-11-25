"""Live Activities (1.30.0): start / update / end decisions and the two registration endpoints.

APNs is mocked (no network); the DB is a throwaway temp dir.

Run:  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_liveactivity.py
"""
import asyncio
import importlib
import os
import tempfile

os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_la_")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
os.environ["APEX_SECRET_KEY"] = "testsecret"

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
importlib.reload(m)
from app import apns, db, liveactivity  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


sent = []
reply = {"result": (True, "ok")}


async def fake_send(token, environment, payload, priority=10):
    sent.append((token, payload, priority))
    return reply.get(token, reply["result"])

apns.send_liveactivity = fake_send
liveactivity.apns.send_liveactivity = fake_send

DEV = "a" * 64
OTHER = "b" * 64
START = "c" * 64
ACT = "d" * 64
CODE = os.environ["PAIRING_CODE"]

with TestClient(m.app) as c:
    db.upsert_device(DEV, m._canonical_code(CODE), "production", "ios")

    # --- registration endpoints
    r = c.post("/v1/liveactivity/register", json={"pairing_code": "WRONG-CODE", "device_token": DEV, "start_token": START})
    check("register refuses a wrong pairing code", r.status_code == 403)
    r = c.post("/v1/liveactivity/register", json={"pairing_code": CODE, "device_token": OTHER, "start_token": START})
    check("register refuses a phone that isn't registered for push", r.status_code == 404)
    r = c.post("/v1/liveactivity/register", json={"pairing_code": CODE, "device_token": DEV, "start_token": "zz-not-hex"})
    check("register refuses a non-hex token", r.status_code == 422)
    r = c.post("/v1/liveactivity/register", json={"pairing_code": CODE, "device_token": DEV, "start_token": START})
    check("register stores the push-to-start token", r.status_code == 200 and db.la_start_token_for(DEV)["start_token"] == START)

    def alert(labels, camera="doorbell", title="Person at Doorbell", review="rev1"):
        asyncio.run(liveactivity.on_person_alert([DEV], camera, "Doorbell", title, "3:42 PM", review, labels))

    # --- start
    sent.clear()
    alert(["car"])
    check("no Live Activity for a car-only alert", not sent)
    alert(["person-verified"])
    check("a person alert starts one", len(sent) == 1 and sent[0][0] == START)
    aps = sent[0][1]["aps"]
    check("start payload names the ActivityKit type", aps.get("attributes-type") == "ApexAlertAttributes"
          and aps.get("attributes") == {"camera": "doorbell", "cameraName": "Doorbell"})
    check("start content-state keys match the Swift ContentState",
          set(aps["content-state"]) == {"title", "body", "reviewID", "lastSeen"})
    check("start carries a stale-date and a silent alert", aps.get("stale-date", 0) > aps["timestamp"]
          and "sound" not in aps.get("alert", {}) and aps.get("event") == "start")

    sent.clear()
    alert(["person"], review="rev2")
    check("a second person alert before the app reports its token doesn't start a duplicate", not sent)

    # --- update via the per-activity token
    r = c.post("/v1/liveactivity/activity", json={"pairing_code": CODE, "device_token": DEV, "camera": "doorbell",
                                                   "activity_token": ACT})
    check("activity token stored", r.status_code == 200 and db.la_activity_for(DEV, "doorbell")["activity_token"] == ACT)
    sent.clear()
    alert(["person"], title="Person still at Doorbell", review="rev3")
    check("next person alert updates the showing activity", len(sent) == 1 and sent[0][0] == ACT
          and sent[0][1]["aps"]["event"] == "update"
          and sent[0][1]["aps"]["content-state"]["title"] == "Person still at Doorbell")

    # --- a dead activity token: start a fresh one
    sent.clear()
    reply[ACT] = (False, "410 Unregistered")      # the activity is gone; the start token is fine
    alert(["person"], review="rev4")
    del reply[ACT]
    check("410 on update falls back to a fresh start", [p[1]["aps"]["event"] for p in sent] == ["update", "start"])

    # --- a different camera is its own activity
    sent.clear()
    alert(["person"], camera="Side_Gate", review="rev5")
    check("another camera gets its own activity", len(sent) == 1 and sent[0][1]["aps"]["attributes"]["camera"] == "Side_Gate")

    # --- sweeper ends quiet ones
    c.post("/v1/liveactivity/activity", json={"pairing_code": CODE, "device_token": DEV, "camera": "doorbell",
                                              "activity_token": ACT})
    sent.clear()
    closed = asyncio.run(liveactivity.sweep_once(now=10**10))
    ends = [p for p in sent if p[1]["aps"]["event"] == "end"]
    check("sweeper ends a quiet activity with a dismissal-date", len(ends) == 1 and ends[0][0] == ACT
          and "dismissal-date" in ends[0][1]["aps"] and ends[0][2] == 5)
    check("...and forgets every quiet row", closed == 2 and db.la_activity_for(DEV, "doorbell") is None
          and db.la_activity_for(DEV, "Side_Gate") is None)

    sent.clear()
    closed = asyncio.run(liveactivity.sweep_once())
    check("sweeper leaves nothing to do right after", closed == 0 and not sent)

    # --- turning it off
    r = c.post("/v1/liveactivity/register", json={"pairing_code": CODE, "device_token": DEV, "start_token": ""})
    check("empty start token turns Live Activities off", r.status_code == 200 and db.la_start_token_for(DEV) is None)
    sent.clear()
    alert(["person"], review="rev6")
    check("no Live Activity once it's off", not sent)

    # --- notify hook: only first-stage alerts, only delivered phones
    called = []

    async def fake_on_alert(*args):
        called.append(args)

    liveactivity.on_person_alert = fake_on_alert
    m.liveactivity.on_person_alert = fake_on_alert

    async def fake_deliver(code, payload, collapse_id="", gate=None):
        return {"devices": 1, "sent": 1, "failed": 0, "pruned": 0, "suppressed": 0, "errors": [], "delivered": [DEV]}

    m.apns.deliver_to_pairing = fake_deliver
    m.apns.is_configured = lambda: True
    base = {"pairing_code": CODE, "title": "Person", "body": "x", "camera": "doorbell", "review_id": "r9",
            "labels": ["person"]}
    c.post("/v1/notify", json=base)
    c.post("/v1/notify", json={**base, "review_id": "r10", "silent": True})
    c.post("/v1/notify", json={**base, "review_id": "r11", "is_description": True})
    check("notify hands only the first-stage alert to Live Activities",
          len(called) == 1 and called[0][0] == [DEV] and called[0][1] == "doorbell")

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
