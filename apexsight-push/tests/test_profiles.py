"""Regression tests for the Frigate-profile path in the bridge.

Run:  PYTHONPATH=. python3 tests/test_profiles.py

Switching house modes used to mean 18 Home Assistant switch calls (2 per camera x 9). A Frigate
0.18 profile does the same thing in ONE `frigate/profile/set` publish, and applies atomically so
there is no window where half the cameras have flipped.

The load-bearing rule is WHEN it may be used. The profiles baked into Frigate's config encode the
DEFAULT matrix, so they cannot represent a matrix the household edited in the app. Getting that
wrong is a security bug in the dangerous direction: cameras the user muted would start alerting,
or worse, cameras they expect to alert would go quiet. Hence `is_default` gates everything.
"""
import json
import os

os.environ.setdefault("PAIRING_CODE", "APEX-PLEX-5250")
import bridge  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


DEFAULTS = bridge._MODE_MUTES_DEFAULT
_store = {}
bridge._get_cfg = lambda k, d="": _store.get(k, d)          # stub the DB


def set_map(m):
    _store["mode_map"] = json.dumps(m) if m is not None else ""


# ---- is_default detection: the gate for using a profile ----
set_map(None)
for mode in ("home", "night", "away"):
    muted, roster, is_default = bridge._effective_mutes(mode)
    check(f"no custom map -> '{mode}' is default", is_default)
    check(f"no custom map -> '{mode}' mutes match the built-ins", sorted(muted) == sorted(DEFAULTS[mode]))
    check(f"'{mode}' falls back to the full camera roster", len(roster) > 0)

# A custom map that HAPPENS to equal the defaults is still safe to express as a profile.
set_map({"home": list(DEFAULTS["home"])})
_, _, is_default = bridge._effective_mutes("home")
check("custom map identical to defaults still counts as default", is_default)

# Order must not matter — the app may store the list in any order.
set_map({"home": list(reversed(DEFAULTS["home"]))})
_, _, is_default = bridge._effective_mutes("home")
check("reordered custom list still counts as default (compared as a set)", is_default)

# A genuinely edited list must NOT use a profile.
set_map({"home": ["Garage"]})
muted, _, is_default = bridge._effective_mutes("home")
check("EDITED map is NOT default -> must use switches, never a profile", not is_default)
check("edited map's mutes are honoured verbatim", muted == ["Garage"])

# An edit to one mode must not mark the others custom.
set_map({"home": ["Garage"]})
_, _, night_default = bridge._effective_mutes("night")
check("editing 'home' leaves 'night' on the default path", night_default)

# Adding a camera to the default list is a real edit.
set_map({"night": DEFAULTS["night"] + ["Garage"]})
_, _, is_default = bridge._effective_mutes("night")
check("adding a camera to a mode's list is NOT default", not is_default)

# Malformed input must fail safe: treat as default rather than crash mid mode-change.
_store["mode_map"] = "{not json"
_, _, is_default = bridge._effective_mutes("home")
check("malformed mode_map falls back to defaults without raising", is_default)
set_map({"home": "not-a-list"})
muted, _, is_default = bridge._effective_mutes("home")
check("non-list value for a mode falls back to the built-in defaults", sorted(muted) == sorted(DEFAULTS["home"]))
check("non-list value still counts as default", is_default)

# ---- publishing ----
class FakeClient:
    def __init__(self, connected=True, rc=0):
        self.connected, self.rc, self.sent = connected, rc, []

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, qos, retain))
        return type("I", (), {"rc": self.rc})()


c = FakeClient()
check("publish returns True on success", bridge._set_frigate_profile(c, "home"))
check("publishes to frigate/profile/set", c.sent[0][0] == "frigate/profile/set")
check("payload is the bare mode name", c.sent[0][1] == "home")
check("published at qos=1 so the broker retries the handoff", c.sent[0][2] == 1)
check("NOT retained (a retained mode would be re-applied on every reconnect)", c.sent[0][3] is False)

check("returns False when MQTT is disconnected -> caller falls back to switches",
      not bridge._set_frigate_profile(FakeClient(connected=False), "home"))
check("returns False when the broker rejects the publish",
      not bridge._set_frigate_profile(FakeClient(rc=4), "home"))
check("returns False with no client at all", not bridge._set_frigate_profile(None, "home"))

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
