"""Regression tests for app/gate.py — the per-device notification delivery gate and the
house-mode camera mute resolver. FAIL-OPEN IS LAW here (see gate.py's own module docstring):
every test below either confirms an affirmative mute suppresses, or confirms that anything
short of an affirmative, well-formed mute delivers.

Run:  PYTHONPATH=. python3 tests/test_gate.py
(Not plain pytest — see test_security.py's own note; this file follows the same script style
for consistency with the existing suite.)
"""
import os, tempfile

os.environ.setdefault("APEX_DATA_DIR", tempfile.mkdtemp(prefix="apextest_gate_"))

from app import gate

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


NOW = 1_700_000_000.0  # arbitrary fixed epoch so tests are deterministic

# ---- would_deliver: malformed / absent input always fails open --------------

check("non-dict prefs -> deliver", gate.would_deliver(None, "front_door", "person", [], 0.9, NOW)[0])
check("non-dict prefs (list) -> deliver", gate.would_deliver([1, 2], "front_door", "person", [], 0.9, NOW)[0])
check("empty prefs -> deliver", gate.would_deliver({}, "front_door", "person", [], 0.9, NOW)[0])

# ---- Hard mutes: only an affirmative parsed value suppresses ----------------

check("disarmed:true -> suppress", not gate.would_deliver({"disarmed": True}, "front_door", "person", [], 0.9, NOW)[0])
check("disarmed:'true' (string, not bool) -> deliver (fail-open, not parsed as True)",
      gate.would_deliver({"disarmed": "true"}, "front_door", "person", [], 0.9, NOW)[0])
check("disarmed:false -> deliver", gate.would_deliver({"disarmed": False}, "front_door", "person", [], 0.9, NOW)[0])

check("global snooze in the future -> suppress",
      not gate.would_deliver({"snoozed_until": NOW + 3600}, "front_door", "person", [], 0.9, NOW)[0])
check("global snooze in the past -> deliver (expired)",
      gate.would_deliver({"snoozed_until": NOW - 1}, "front_door", "person", [], 0.9, NOW)[0])
check("global snooze malformed (non-numeric) -> deliver",
      gate.would_deliver({"snoozed_until": "not-a-number"}, "front_door", "person", [], 0.9, NOW)[0])

check("camera snooze in the future for THIS camera -> suppress",
      not gate.would_deliver({"camera_snoozes": {"front_door": NOW + 60}}, "front_door", "person", [], 0.9, NOW)[0])
check("camera snooze for a DIFFERENT camera -> deliver",
      gate.would_deliver({"camera_snoozes": {"backyard": NOW + 60}}, "front_door", "person", [], 0.9, NOW)[0])
check("camera snooze expired -> deliver",
      gate.would_deliver({"camera_snoozes": {"front_door": NOW - 60}}, "front_door", "person", [], 0.9, NOW)[0])
check("camera_snoozes not a dict -> deliver (fail-open)",
      gate.would_deliver({"camera_snoozes": "bogus"}, "front_door", "person", [], 0.9, NOW)[0])

# ---- Soft filters: camera / object / zone -----------------------------------

check("camera disabled -> suppress",
      not gate.would_deliver({"cameras_disabled": ["front_door"]}, "front_door", "person", [], 0.9, NOW)[0])
check("object disabled -> suppress",
      not gate.would_deliver({"objects_disabled": ["person"]}, "front_door", "person", [], 0.9, NOW)[0])
check("zone disabled (event's only zone) -> suppress",
      not gate.would_deliver({"zones_disabled": ["porch"]}, "front_door", "person", ["porch"], 0.9, NOW)[0])
check("zone disabled but event has NO zones -> deliver (fail-open: zone_ok when zones empty)",
      gate.would_deliver({"zones_disabled": ["porch"]}, "front_door", "person", [], 0.9, NOW)[0])
check("event has multiple zones, only one disabled -> deliver (any enabled zone passes)",
      gate.would_deliver({"zones_disabled": ["porch"]}, "front_door", "person", ["porch", "driveway"], 0.9, NOW)[0])
check("nothing disabled -> deliver",
      gate.would_deliver({"cameras_disabled": [], "objects_disabled": [], "zones_disabled": []},
                          "front_door", "person", [], 0.9, NOW)[0])

# ---- Quiet hours -------------------------------------------------------------

same_day = {"quiet_hours": {"enabled": True, "start": 60, "end": 300}, "tz_offset": 0}  # 01:00-05:00 UTC
check("quiet hours same-day window, inside -> suppress",
      not gate.would_deliver(same_day, "front_door", "person", [], 0.9, NOW - (NOW % 86400) + 120 * 60)[0])
check("quiet hours disabled flag off -> deliver even 'inside' the window (nothing else muted)",
      gate.would_deliver({"quiet_hours": {"enabled": False, "start": 60, "end": 300}, "tz_offset": 0},
                          "front_door", "person", [], 0.9, NOW - (NOW % 86400) + 120 * 60)[0])
check("quiet hours malformed start/end -> not quiet (fail-open), deliver",
      gate.would_deliver({"quiet_hours": {"enabled": True, "start": "bad", "end": 300}, "tz_offset": 0},
                          "front_door", "person", [], 0.9, NOW)[0])
check("quiet hours start==end (degenerate) -> not quiet, deliver",
      gate.would_deliver({"quiet_hours": {"enabled": True, "start": 100, "end": 100}, "tz_offset": 0},
                          "front_door", "person", [], 0.9, NOW)[0])
# Overnight window 22:00-06:00 UTC: minute 1350 (22:30) is inside.
midnight_epoch = NOW - (NOW % 86400)
check("quiet hours overnight window, inside (late night) -> suppress",
      not gate.would_deliver({"quiet_hours": {"enabled": True, "start": 1320, "end": 360}, "tz_offset": 0},
                              "front_door", "person", [], 0.9,
                              midnight_epoch + 1350 * 60)[0])
check("quiet hours overnight window, outside (midday) -> deliver",
      gate.would_deliver({"quiet_hours": {"enabled": True, "start": 1320, "end": 360}, "tz_offset": 0},
                          "front_door", "person", [], 0.9,
                          midnight_epoch + 720 * 60)[0])

# ---- Triggers are additive allow-rules (can re-open a soft mute) ------------

muted_but_triggered = {
    "cameras_disabled": ["front_door"],
    "triggers": [{"name": "porch-person", "cameras": ["front_door"], "labels": ["person"],
                  "required_zones": [], "min_confidence": 0.5, "enabled": True}],
}
check("trigger matching camera+label re-opens a camera-muted event -> deliver",
      gate.would_deliver(muted_but_triggered, "front_door", "person", [], 0.9, NOW)[0])
check("trigger for a DIFFERENT camera does not re-open -> suppress",
      not gate.would_deliver({**muted_but_triggered,
                               "triggers": [{"name": "x", "cameras": ["backyard"], "labels": ["person"],
                                             "required_zones": [], "enabled": True}]},
                              "front_door", "person", [], 0.9, NOW)[0])
check("trigger below min_confidence does not re-open -> suppress",
      not gate.would_deliver({**muted_but_triggered,
                               "triggers": [{"name": "x", "cameras": ["front_door"], "labels": ["person"],
                                             "required_zones": [], "min_confidence": 0.95, "enabled": True}]},
                              "front_door", "person", [], 0.5, NOW)[0])
check("disabled trigger does not re-open -> suppress",
      not gate.would_deliver({**muted_but_triggered,
                               "triggers": [{"name": "x", "cameras": ["front_door"], "labels": ["person"],
                                             "required_zones": [], "enabled": False}]},
                              "front_door", "person", [], 0.9, NOW)[0])
check("trigger requiring a zone the event lacks does not re-open -> suppress",
      not gate.would_deliver({**muted_but_triggered,
                               "triggers": [{"name": "x", "cameras": ["front_door"], "labels": ["person"],
                                             "required_zones": ["porch"], "enabled": True}]},
                              "front_door", "person", [], 0.9, NOW)[0])
check("trigger with respect_quiet_hours=True does not re-open during quiet hours -> suppress",
      not gate.would_deliver({
          "cameras_disabled": ["front_door"],
          "quiet_hours": {"enabled": True, "start": 0, "end": 1439}, "tz_offset": 0,
          "triggers": [{"name": "x", "cameras": ["front_door"], "labels": ["person"],
                        "required_zones": [], "enabled": True, "respect_quiet_hours": True}],
      }, "front_door", "person", [], 0.9, midnight_epoch + 60)[0])
check("trigger with respect_quiet_hours=False re-opens even during quiet hours -> deliver",
      gate.would_deliver({
          "cameras_disabled": ["front_door"],
          "quiet_hours": {"enabled": True, "start": 0, "end": 1439}, "tz_offset": 0,
          "triggers": [{"name": "x", "cameras": ["front_door"], "labels": ["person"],
                        "required_zones": [], "enabled": True, "respect_quiet_hours": False}],
      }, "front_door", "person", [], 0.9, midnight_epoch + 60)[0])

# ---- Exception safety: even a malformed/hostile structure fails open -------

check("triggers is not a list -> no crash, deliver (soft filters already passed)",
      gate.would_deliver({"triggers": "not-a-list"}, "front_door", "person", [], 0.9, NOW)[0])
check("triggers contains non-dict entries -> skipped safely, camera mute still suppresses",
      not gate.would_deliver({"cameras_disabled": ["front_door"], "triggers": ["not-a-dict", None, 42]},
                              "front_door", "person", [], 0.9, NOW)[0])

# ---- mode_mutes_camera / resolve_mode_mutes (house-mode filter) ------------

check("home mode mutes Garage", gate.mode_mutes_camera("home", "Garage"))
check("home mode does not mute Front_Driveway", not gate.mode_mutes_camera("home", "Front_Driveway"))
check("away mode mutes nothing", not gate.mode_mutes_camera("away", "Garage"))
check("unknown mode mutes nothing (fail-open)", not gate.mode_mutes_camera("vacation", "Garage"))
check("blank mode mutes nothing (fail-open)", not gate.mode_mutes_camera("", "Garage"))
check("no camera name -> never muted", not gate.mode_mutes_camera("home", ""))

custom = {"home": ["OnlyThisCam"]}
check("custom map overrides defaults for the given mode",
      gate.mode_mutes_camera("home", "OnlyThisCam", custom))
check("custom map for a mode means the DEFAULT list no longer applies",
      not gate.mode_mutes_camera("home", "Garage", custom))
check("custom map absent for a mode falls back to defaults",
      gate.mode_mutes_camera("night", "Living_Room_Wide", custom))
check("resolve_mode_mutes returns custom list when present",
      gate.resolve_mode_mutes("home", custom) == ["OnlyThisCam"])
check("resolve_mode_mutes falls back to defaults when custom_map has no list for this mode",
      gate.resolve_mode_mutes("away", custom) == [])
check("resolve_mode_mutes ignores a non-list custom value",
      gate.resolve_mode_mutes("home", {"home": "not-a-list"}) == gate.MODE_MUTES["home"])

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
