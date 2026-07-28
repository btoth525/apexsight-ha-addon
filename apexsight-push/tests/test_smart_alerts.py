"""Tests for AI-rated alerts — the rules that decide whether a phone interrupts someone.

Run:  PYTHONPATH=. python3 tests/test_smart_alerts.py

The follow-up push already REPLACES the instant one via a shared collapse id, so whatever it
carries is what the user is left looking at. It now carries Frigate's review summary, which means
the notification says what happened ("Package Delivery at Residence") instead of "Person — Doorbell".

The load-bearing rule is the interruption level:
  * routine (0)      -> stays PASSIVE. A delivery must not buzz you twice.
  * notable (1)      -> time-sensitive, breaks through Focus, but no sound.
  * concerning (2)   -> time-sensitive AND audible.
Both directions are bugs: buzzing on every delivery trains people to ignore the app, and staying
silent on something worth seeing defeats the point of a security app. FAIL-QUIET everywhere — an
unparseable level reads as routine, because a language model produced that number.
"""
import os, tempfile

# apns -> db -> config creates DATA_DIR at import time; point it somewhere writable, exactly as
# the other suites do.
os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apexsmart_")
os.environ.setdefault("PAIRING_CODE", "APEX-PLEX-5250")
os.environ["APEX_SECRET_KEY"] = "testsecret"
from app import apns  # noqa: E402
import bridge  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


def aps(**kw):
    return apns.build_payload(title="t", body="b", review_id="r1", camera="doorbell", **kw)["aps"]


# ---- interruption level ----
p = aps(silent=True, threat_level=0)
check("routine follow-up stays passive (no second buzz for a delivery)",
      p.get("interruption-level") == "passive" and "sound" not in p)

p = aps(silent=True, threat_level=1)
check("notable OVERRIDES silent -> time-sensitive", p.get("interruption-level") == "time-sensitive")
check("notable stays silent-but-visible (no sound)", "sound" not in p)

p = aps(silent=True, threat_level=2)
check("concerning -> time-sensitive", p.get("interruption-level") == "time-sensitive")
check("concerning is audible", p.get("sound") == "default")

p = aps(silent=False, threat_level=0)
check("a normal first alert is unchanged (sound, no override)",
      p.get("sound") == "default" and "interruption-level" not in p)

# ---- payload passthrough ----
full = apns.build_payload(title="t", body="b", review_id="r1", camera="doorbell",
                          silent=True, threat_level=2, ai_summary="Someone tried the door handle.",
                          ai_concerns="Trying door handles")
check("threat_level reaches the app", full.get("threat_level") == 2)
check("ai_summary reaches the app", full.get("ai_summary") == "Someone tried the door handle.")
check("ai_concerns reaches the app", full.get("ai_concerns") == "Trying door handles")
check("a replacing push still suppresses the badge bump", full.get("no_badge") is True)

quiet = apns.build_payload(title="t", body="b", review_id="r1", silent=True, threat_level=0)
check("threat_level 0 is omitted rather than sent as noise", "threat_level" not in quiet)

longsum = apns.build_payload(title="t", body="b", review_id="r1", ai_summary="x" * 900)
check("ai_summary is bounded so a runaway model can't bloat the push",
      len(longsum["ai_summary"]) <= 400)

# ---- level clamping (fail quiet) ----
check("nil metadata -> routine", bridge._threat_level(None) == 0)
check("missing key -> routine", bridge._threat_level({}) == 0)
check("explicit 0 -> routine", bridge._threat_level({"potential_threat_level": 0}) == 0)
check("1 -> notable", bridge._threat_level({"potential_threat_level": 1}) == 1)
check("2 -> concerning", bridge._threat_level({"potential_threat_level": 2}) == 2)
check("above the top saturates rather than vanishing",
      bridge._threat_level({"potential_threat_level": 99}) == 2)
check("negative fails quiet to routine",
      bridge._threat_level({"potential_threat_level": -3}) == 0)
check("non-numeric fails quiet to routine",
      bridge._threat_level({"potential_threat_level": "high"}) == 0)
check("None value fails quiet to routine",
      bridge._threat_level({"potential_threat_level": None}) == 0)

# ---- the story fetch must never block or break an alert ----
check("no review id -> None immediately", bridge._review_ai_story("") is None)

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
