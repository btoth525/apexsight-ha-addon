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

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
