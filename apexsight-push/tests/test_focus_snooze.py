"""End-to-end tests for the PER-DEVICE Focus mute and the household-gate attribution.

Run:  PYTHONPATH=. python3 tests/test_focus_snooze.py

The bug these exist to prevent: the iOS Focus filter used to write the HOUSEHOLD gate
(/v1/gate), so one partner's Do Not Disturb turning on silenced every phone's camera alerts
for eight hours, invisibly. A Focus belongs to one person's device. These pin:

  1. A Focus mute is stored per DEVICE TOKEN and never touches gate:{code}.
  2. Posting a Focus mute cannot wipe that device's soft prefs (they're separate keys —
     the two are written by different processes and /v1/device-prefs REPLACES what it stores).
  3. A soft-prefs sync cannot clear a live Focus mute, and vice versa.
  4. The horizon is clamped, so a stuck Focus can't mute a phone forever.
  5. The household gate records WHO silenced it and WHEN, so the app's banner can say so.
"""
import os, tempfile, importlib, json, time
os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apexfocus_")
os.environ.setdefault("PAIRING_CODE", "APEX-PLEX-5250")
os.environ["APEX_SECRET_KEY"] = "testsecret"
from fastapi.testclient import TestClient

ok = []
def check(name, cond):
    ok.append(bool(cond)); print(("PASS" if cond else "FAIL"), name)

import app.main as m
import app.db as db
importlib.reload(m)

CODE = "APEX-PLEX-5250"
TOK = "a" * 64          # this phone (e.g. the wife's, whose Do Not Disturb is on)
OTHER = "b" * 64        # the other phone in the household

SOFT_PREFS = {
    "cameras_disabled": ["Garage"],
    "quiet_hours": {"enabled": True, "start": 1320, "end": 420},
    "triggers": [{"name": "pkg", "enabled": True, "cameras": [], "labels": ["package"]}],
}

with TestClient(m.app) as c:
    # ---- 1. A Focus mute is per-device and never touches the household gate ----
    c.post("/v1/gate", json={"pairing_code": CODE, "disarmed": False, "snoozed_until": 0})
    until = time.time() + 8 * 3600
    r = c.post("/v1/device-prefs", json={
        "device_token": TOK, "pairing_code": CODE, "focus_snoozed_until": until,
    })
    check("focus mute accepted -> 200", r.status_code == 200)
    check("focus mute stored under its own per-device key",
          json.loads(db.get_config(f"focus:{TOK}"))["until"] > time.time())
    gate_raw = db.get_config(f"gate:{CODE}") or "{}"
    g = json.loads(gate_raw)
    check("focus mute did NOT set the household snooze (THE bug)",
          not (g.get("snoozed_until") or 0))
    check("focus mute did NOT disarm the household", not g.get("disarmed"))
    check("focus mute did NOT leak into the OTHER phone's prefs",
          not db.get_config(f"focus:{OTHER}"))

    # ---- 2. A Focus-only post must not wipe the device's soft prefs ----
    c.post("/v1/device-prefs", json={
        "device_token": TOK, "pairing_code": CODE, "prefs": SOFT_PREFS,
    })
    check("soft prefs stored",
          json.loads(db.get_config(f"prefs:{TOK}"))["cameras_disabled"] == ["Garage"])
    c.post("/v1/device-prefs", json={
        "device_token": TOK, "pairing_code": CODE, "focus_snoozed_until": time.time() + 3600,
    })
    stored = json.loads(db.get_config(f"prefs:{TOK}"))
    check("a Focus-only post leaves camera mutes intact",
          stored.get("cameras_disabled") == ["Garage"])
    check("a Focus-only post leaves triggers intact", len(stored.get("triggers") or []) == 1)
    check("a Focus-only post leaves quiet hours intact",
          (stored.get("quiet_hours") or {}).get("enabled") is True)

    # ---- 3. Neither write clears the other ----
    c.post("/v1/device-prefs", json={
        "device_token": TOK, "pairing_code": CODE, "prefs": SOFT_PREFS,
    })
    check("a soft-prefs sync (no focus field) leaves a live Focus mute alone",
          json.loads(db.get_config(f"focus:{TOK}"))["until"] > time.time())
    check("focus_snoozed_until is stripped from the soft blob (single source of truth)",
          "focus_snoozed_until" not in json.loads(db.get_config(f"prefs:{TOK}")))

    # A full sync CAN clear it — that's how the app heals a mute the extension failed to clear.
    c.post("/v1/device-prefs", json={
        "device_token": TOK, "pairing_code": CODE, "prefs": SOFT_PREFS, "focus_snoozed_until": 0,
    })
    check("an explicit 0 from the app clears the Focus mute",
          json.loads(db.get_config(f"focus:{TOK}"))["until"] == 0)

    # ---- 4. Horizon is clamped ----
    c.post("/v1/device-prefs", json={
        "device_token": TOK, "pairing_code": CODE,
        "focus_snoozed_until": time.time() + 400 * 24 * 3600,
    })
    clamped = json.loads(db.get_config(f"focus:{TOK}"))["until"]
    check("a far-future Focus mute is clamped to <= 24h",
          clamped <= time.time() + 24 * 3600 + 5)
    c.post("/v1/device-prefs", json={
        "device_token": TOK, "pairing_code": CODE, "focus_snoozed_until": -99999,
    })
    check("a negative Focus mute floors to 0",
          json.loads(db.get_config(f"focus:{TOK}"))["until"] == 0)

    # ---- 5. Household gate attribution ----
    snooze_until = time.time() + 3600
    c.post("/v1/gate", json={"pairing_code": CODE, "disarmed": False,
                             "snoozed_until": snooze_until, "by": "Brandons Iphone"})
    r = c.get("/v1/mode", params={"pairing_code": CODE}).json()
    check("GET /v1/mode reports who set the household snooze",
          r.get("gate_by") == "Brandons Iphone")
    check("GET /v1/mode reports when it was set", (r.get("gate_at") or 0) > 0)

    # Resuming must not leave a name behind implying someone muted something.
    c.post("/v1/gate", json={"pairing_code": CODE, "disarmed": False, "snoozed_until": 0,
                             "by": "Brandons Iphone"})
    r = c.get("/v1/mode", params={"pairing_code": CODE}).json()
    check("a resume clears the attribution", not r.get("gate_by"))
    check("a resume clears the snooze", (r.get("snoozed_until") or 0) == 0)

    # Attribution is optional — an older app that omits `by` must still be accepted.
    r = c.post("/v1/gate", json={"pairing_code": CODE, "disarmed": False,
                                 "snoozed_until": time.time() + 600})
    check("gate POST without `by` still succeeds (older app builds)", r.status_code == 200)
    check("attribution absent reads as empty, not an error",
          c.get("/v1/mode", params={"pairing_code": CODE}).json().get("gate_by") == "")

    # A wrong pairing code still can't silence the house.
    check("focus mute requires the household code",
          c.post("/v1/device-prefs", json={"device_token": TOK, "pairing_code": "WRONG",
                                           "focus_snoozed_until": time.time() + 60}
                 ).status_code == 403)

    # ---- 6. The dedicated /v1/focus-mute endpoint (what the widget extension posts to) ----
    # It exists so the extension never sends a partial body to /v1/device-prefs, where an older
    # relay would read the absent `prefs` as {} and wipe this phone's soft prefs.
    c.post("/v1/device-prefs", json={"device_token": TOK, "pairing_code": CODE, "prefs": SOFT_PREFS})
    # Compare the gate before/after rather than asserting it's empty — earlier sections leave an
    # active household snooze behind, and "unchanged" is the invariant that actually matters here.
    gate_before = db.get_config(f"gate:{CODE}")
    r = c.post("/v1/focus-mute", json={"device_token": TOK, "pairing_code": CODE,
                                       "until": time.time() + 8 * 3600})
    check("/v1/focus-mute accepted -> 200", r.status_code == 200)
    check("/v1/focus-mute writes the same per-device key",
          json.loads(db.get_config(f"focus:{TOK}"))["until"] > time.time())
    check("/v1/focus-mute does NOT touch the soft-prefs blob",
          json.loads(db.get_config(f"prefs:{TOK}")).get("cameras_disabled") == ["Garage"])
    check("/v1/focus-mute leaves the household gate byte-for-byte unchanged",
          db.get_config(f"gate:{CODE}") == gate_before)
    check("/v1/focus-mute requires the household code",
          c.post("/v1/focus-mute", json={"device_token": TOK, "pairing_code": "WRONG",
                                         "until": time.time() + 60}).status_code == 403)
    check("/v1/focus-mute clamps a far-future value",
          json.loads(c.post("/v1/focus-mute", json={
              "device_token": TOK, "pairing_code": CODE,
              "until": time.time() + 400 * 24 * 3600}).text)["until"] <= time.time() + 24 * 3600 + 5)
    c.post("/v1/focus-mute", json={"device_token": TOK, "pairing_code": CODE, "until": 0})
    check("/v1/focus-mute with 0 resumes this device",
          json.loads(db.get_config(f"focus:{TOK}"))["until"] == 0)

    # ---- 7. BACKWARD COMPATIBILITY with app build 212 -------------------------------------
    # Both phones run 212 the moment this add-on updates, and 212 knows nothing about `by`,
    # `focus_snoozed_until` or /v1/focus-mute. Its request shapes must keep working, and — the
    # one that would actually hurt — a 212 soft-prefs sync must not disturb a live Focus mute.
    OLD = "c" * 64
    r = c.post("/v1/gate", json={"pairing_code": CODE, "disarmed": False,
                                 "snoozed_until": time.time() + 300})
    check("212-shaped /v1/gate (no `by`) -> 200", r.status_code == 200)
    check("212-shaped /v1/gate still stores the snooze",
          json.loads(db.get_config(f"gate:{CODE}"))["snoozed_until"] > time.time())

    c.post("/v1/focus-mute", json={"device_token": OLD, "pairing_code": CODE,
                                   "until": time.time() + 3600})
    r = c.post("/v1/device-prefs", json={
        "device_token": OLD, "pairing_code": CODE,
        "device_name": "Brandons Iphone", "prefs": SOFT_PREFS,
    })
    check("212-shaped /v1/device-prefs (no focus field) -> 200", r.status_code == 200)
    check("212-shaped /v1/device-prefs still stores soft prefs",
          json.loads(db.get_config(f"prefs:{OLD}"))["cameras_disabled"] == ["Garage"])
    check("212-shaped /v1/device-prefs does NOT disturb a live Focus mute",
          json.loads(db.get_config(f"focus:{OLD}"))["until"] > time.time())

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
