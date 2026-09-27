"""Regression tests for app/apns.py's dead-token pruning classification and delivery counting.

The dangerous regression direction: a false-positive prune. Some transient failure whose detail
string happens to contain one of the "permanent" substrings ("410", "BadDeviceToken",
"Unregistered", "BadEnvironmentKeyInToken") gets misread as permanent, and a LIVE device is
silently deleted — a fail-closed alert loss for that phone from then on. These tests mock
send_to_token and the db layer so no real network/DB is involved; they exercise deliver_to_pairing's
actual classification + prune-call decision end to end.

Run:  PYTHONPATH=. python3 tests/test_apns.py
"""
import asyncio
import os
import tempfile

os.environ.setdefault("APEX_DATA_DIR", tempfile.mkdtemp(prefix="apextest_apns_"))

from app import apns, db

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


def run(coro):
    return asyncio.run(coro)


def with_devices(rows, sends, deletes_recorded):
    """Monkeypatch db.devices_for to return `rows`, apns.send_to_token to return the next
    (ok, detail) from `sends` (keyed by token), and db.delete_device_if_unchanged to just record
    its calls into `deletes_recorded` instead of touching a real DB."""
    orig_devices_for = db.devices_for
    orig_send = apns.send_to_token
    orig_delete = db.delete_device_if_unchanged

    def fake_devices_for(pairing_code):
        return rows

    async def fake_send(device_token, environment, payload, collapse_id=""):
        return sends[device_token]

    def fake_delete(device_token, expected_updated_at):
        deletes_recorded.append((device_token, expected_updated_at))

    db.devices_for = fake_devices_for
    apns.send_to_token = fake_send
    db.delete_device_if_unchanged = fake_delete
    return orig_devices_for, orig_send, orig_delete


def restore(orig_devices_for, orig_send, orig_delete):
    db.devices_for = orig_devices_for
    apns.send_to_token = orig_send
    db.delete_device_if_unchanged = orig_delete


# ---- A genuine permanent failure (410/Unregistered) IS pruned ---------------

rows = [{"device_token": "tok-dead", "environment": "production", "updated_at": 1000}]
sends = {"tok-dead": (False, "410 Unregistered")}
deletes = []
saved = with_devices(rows, sends, deletes)
result = run(apns.deliver_to_pairing("APEX-TEST-0001", {"aps": {}}))
restore(*saved)
check("genuine 410/Unregistered -> pruned", deletes == [("tok-dead", 1000)])
check("genuine 410/Unregistered -> counted as pruned+failed, not sent",
      result["pruned"] == 1 and result["failed"] == 1 and result["sent"] == 0)

# ---- BadDeviceToken IS pruned ------------------------------------------------

rows = [{"device_token": "tok-bad", "environment": "production", "updated_at": 500}]
sends = {"tok-bad": (False, "400 BadDeviceToken")}
deletes = []
saved = with_devices(rows, sends, deletes)
run(apns.deliver_to_pairing("APEX-TEST-0001", {"aps": {}}))
restore(*saved)
check("BadDeviceToken -> pruned", deletes == [("tok-bad", 500)])

# ---- BadEnvironmentKeyInToken IS pruned -------------------------------------

rows = [{"device_token": "tok-env", "environment": "production", "updated_at": 42}]
sends = {"tok-env": (False, "403 BadEnvironmentKeyInToken")}
deletes = []
saved = with_devices(rows, sends, deletes)
run(apns.deliver_to_pairing("APEX-TEST-0001", {"aps": {}}))
restore(*saved)
check("BadEnvironmentKeyInToken -> pruned", deletes == [("tok-env", 42)])

# ---- Transient failures are NOT pruned (the dangerous false-positive direction) ----

transient_cases = [
    ("500 Internal Server Error", "5xx server error"),
    ("429 TooManyRequests", "rate limited"),
    ("network error: connection timed out", "network/connection failure"),
    ("403 Forbidden", "generic 403 that isn't BadEnvironmentKeyInToken"),
    ("400 PayloadTooLarge", "a 400 that isn't BadDeviceToken"),
    ("503 ServiceUnavailable", "APNs outage"),
]
for detail, label in transient_cases:
    rows = [{"device_token": "tok-transient", "environment": "production", "updated_at": 99}]
    sends = {"tok-transient": (False, detail)}
    deletes = []
    saved = with_devices(rows, sends, deletes)
    result = run(apns.deliver_to_pairing("APEX-TEST-0001", {"aps": {}}))
    restore(*saved)
    check(f"transient failure NOT pruned: {label} ({detail!r})", deletes == [])
    check(f"transient failure still counted as failed, not silently dropped: {label}",
          result["failed"] == 1 and result["sent"] == 0)

# ---- A successful send is counted, never pruned -----------------------------

rows = [{"device_token": "tok-ok", "environment": "production", "updated_at": 7}]
sends = {"tok-ok": (True, "ok")}
deletes = []
saved = with_devices(rows, sends, deletes)
result = run(apns.deliver_to_pairing("APEX-TEST-0001", {"aps": {}}))
restore(*saved)
check("successful send -> counted, not pruned", result["sent"] == 1 and deletes == [])

# ---- Multiple devices: only the genuinely-dead one is pruned ----------------

rows = [
    {"device_token": "tok-live", "environment": "production", "updated_at": 1},
    {"device_token": "tok-dead2", "environment": "production", "updated_at": 2},
    {"device_token": "tok-flaky", "environment": "production", "updated_at": 3},
]
sends = {
    "tok-live": (True, "ok"),
    "tok-dead2": (False, "410 Unregistered"),
    "tok-flaky": (False, "500 Internal Server Error"),
}
deletes = []
saved = with_devices(rows, sends, deletes)
result = run(apns.deliver_to_pairing("APEX-TEST-0001", {"aps": {}}))
restore(*saved)
check("mixed batch: only the dead token is pruned", deletes == [("tok-dead2", 2)])
check("mixed batch: counts are sent=1/failed=2/pruned=1",
      result["sent"] == 1 and result["failed"] == 2 and result["pruned"] == 1)

# ---- The per-device gate (fail-open by contract) can suppress without pruning ----

rows = [{"device_token": "tok-muted", "environment": "production", "updated_at": 1}]
sends = {"tok-muted": (True, "ok")}  # would have succeeded if not gated
deletes = []
saved = with_devices(rows, sends, deletes)
result = run(apns.deliver_to_pairing("APEX-TEST-0001", {"aps": {}}, gate=lambda token: (False, "muted for test")))
restore(*saved)
check("gate suppression -> counted as suppressed, no send attempted, nothing pruned",
      result["suppressed"] == 1 and result["sent"] == 0 and deletes == [])

# ---- Fan-out is concurrent: phone 2 no longer waits out phone 1's round trip ----

import httpx  # noqa: E402

inflight = {"now": 0, "max": 0}


async def slow_send(device_token, environment, payload, collapse_id=""):
    inflight["now"] += 1
    inflight["max"] = max(inflight["max"], inflight["now"])
    await asyncio.sleep(0.02)
    inflight["now"] -= 1
    return (True, "ok") if device_token != "tok-dead3" else (False, "410 Unregistered")


rows = [
    {"device_token": "tok-a", "environment": "production", "updated_at": 1},
    {"device_token": "tok-muted2", "environment": "production", "updated_at": 2},
    {"device_token": "tok-dead3", "environment": "production", "updated_at": 3},
]
deletes = []
saved = with_devices(rows, {}, deletes)
apns.send_to_token = slow_send
result = run(apns.deliver_to_pairing(
    "APEX-TEST-0001", {"aps": {}}, gate=lambda t: (t != "tok-muted2", "test")))
restore(*saved)
check("every deliverable phone is sent to at once, not one after another", inflight["max"] == 2)
check("concurrent fan-out keeps per-row results aligned (only the dead token pruned)",
      deletes == [("tok-dead3", 3)])
check("concurrent fan-out counts: sent=1 failed=1 pruned=1 suppressed=1",
      (result["sent"], result["failed"], result["pruned"], result["suppressed"]) == (1, 1, 1, 1))

# ---- The APNs connection is kept warm, and a dead kept-alive socket is retried ONCE ----
# httpx's default keepalive is 5 s; alerts are minutes apart, so every push paid a cold handshake.

check("APNs keepalive outlives the gap between alerts", apns._APNS_LIMITS.keepalive_expiry >= 300)
check("a stalled APNs read fails fast (a NAT-dropped socket can't hold a ring for 10 s)",
      apns._APNS_TIMEOUT.read is not None and apns._APNS_TIMEOUT.read <= 5)


class FakeClient:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    async def post(self, url, headers=None, content=None):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return httpx.Response(200, headers={"apns-id": "id-1"})


orig_client, orig_creds = apns._apns_client, apns._credentials
apns._credentials = lambda: ("p8", "KID", "TEAM", "com.example.app", "auto")
orig_token = apns._provider_token
apns._provider_token = lambda p8, kid, team: "jwt"

fake = FakeClient([httpx.ReadError("connection reset")])
apns._apns_client = lambda: fake
ok_, detail, apns_id = run(apns.send_voip("v" * 64, "production", {"aps": {}}))
check("a dead kept-alive socket is retried once and the ring goes out",
      ok_ and fake.calls == 2 and apns_id == "id-1")

fake = FakeClient([httpx.RemoteProtocolError("GOAWAY"), httpx.RemoteProtocolError("GOAWAY")])
apns._apns_client = lambda: fake
ok_, detail = run(apns.send_to_token("t" * 64, "production", {"aps": {}}))
check("the retry is ONCE, then it reports a network error (the bridge retries from there)",
      not ok_ and fake.calls == 2 and detail.startswith("network error"))

fake = FakeClient([httpx.ReadTimeout("slow")])
apns._apns_client = lambda: fake
ok_, detail = run(apns.send_background("t" * 64, "production", {"aps": {}}))
check("a timeout is NOT retried in the relay (that would stack a second stall onto a ring)",
      not ok_ and fake.calls == 1)

apns._apns_client, apns._credentials, apns._provider_token = orig_client, orig_creds, orig_token

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
