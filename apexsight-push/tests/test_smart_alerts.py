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

# ---- traffic-light dot in the title (iOS can't colour a banner, so the colour is a character) ----
check("routine gets a GREEN dot", bridge._LEVEL_DOT[0] == "\U0001F7E2")
check("notable gets a YELLOW dot", bridge._LEVEL_DOT[1] == "\U0001F7E1")
check("concerning gets a RED dot", bridge._LEVEL_DOT[2] == "\U0001F534")
check("all three dots are distinct", len(set(bridge._LEVEL_DOT.values())) == 3)
check("a dot exists for every level the clamp can produce",
      all(l in bridge._LEVEL_DOT for l in (0, 1, 2)))

# ---- the story fetch must never block or break an alert ----
check("no review id -> None immediately", bridge._review_ai_story("") is None)


# ---------------------------------------------------------------------------
# Trusting the rating: a model that isn't sure must not wake the house
# ---------------------------------------------------------------------------
# The real false positive this exists to stop, verbatim from the live server: potential_threat_level
# 2, title "Forced Entry Attempt", an imagined crowbar — raised against objects
# ["person-verified", "person-verified"] (recognised residents carrying a package) at confidence
# 0.02. That would have sent a red, audible, Focus-breaking push about a break-in that never
# happened. Every legitimate Level 1 in the same 61-review sample sat at confidence 0.5-1.0.
#
# The opposite failure matters just as much: a genuine escalation the model IS sure about must
# still get through, and a Level 0 must never need confidence to be believed.

VERIFIED = ["person-verified", "person-verified"]
STRANGER = ["person"]

check("the real hallucination is rejected",
      bridge._trusted_threat_level(
          {"potential_threat_level": 2, "confidence": 0.02}, VERIFIED) is None)
check("a confident escalation on a stranger still fires",
      bridge._trusted_threat_level(
          {"potential_threat_level": 2, "confidence": 0.9}, STRANGER) == 2)
check("a confident notable still fires",
      bridge._trusted_threat_level(
          {"potential_threat_level": 1, "confidence": 0.7}, STRANGER) == 1)
check("low confidence alone rejects an escalation",
      bridge._trusted_threat_level(
          {"potential_threat_level": 1, "confidence": 0.2}, STRANGER) is None)
check("a recognised resident rejects an escalation however confident the model is",
      bridge._trusted_threat_level(
          {"potential_threat_level": 2, "confidence": 1.0}, VERIFIED) is None)
check("exactly at the floor is trusted",
      bridge._trusted_threat_level(
          {"potential_threat_level": 1, "confidence": bridge.CONFIDENCE_FLOOR}, STRANGER) == 1)
check("a missing confidence rejects an escalation",
      bridge._trusted_threat_level({"potential_threat_level": 2}, STRANGER) is None)
check("an unparseable confidence rejects an escalation",
      bridge._trusted_threat_level(
          {"potential_threat_level": 2, "confidence": "very"}, STRANGER) is None)

# Level 0 is exempt: "nothing to see" is the safe answer regardless of how sure the model is, and
# requiring confidence there would turn every quiet review into an unrated one.
check("level 0 needs no confidence",
      bridge._trusted_threat_level({"potential_threat_level": 0}, STRANGER) == 0)
check("level 0 on a verified person is still a clean 0",
      bridge._trusted_threat_level(
          {"potential_threat_level": 0, "confidence": 0.01}, VERIFIED) == 0)
check("no metadata at all is routine, not untrusted",
      bridge._trusted_threat_level(None, None) == 0)

# None must read as UNRATED downstream, never as a green all-clear.
check("untrusted gets no traffic-light dot",
      bridge._LEVEL_DOT.get(None) is None)


# ---------------------------------------------------------------------------
# The summary Frigate cut in half
# ---------------------------------------------------------------------------
# Frigate hard-clamps shortSummary to 140 chars mid-word; 22 of 61 rated reviews ended mid-sentence.
# In all 22 the `scene` field carried the same narrative, finished.
CUT = "An individual is walking towards a parked car on the street. They approach the vehicle but do not enter or interact with it, instead moving "
WHOLE = "A person is walking on the street towards a parked car. They approach the car but do not enter or interact with it, instead moving around the front of the house and along the sidewalk."

check("the complete field wins over the clamped one",
      bridge._story_summary({"shortSummary": CUT, "scene": WHOLE}) == WHOLE)
check("a complete shortSummary is kept when scene is absent",
      bridge._story_summary({"shortSummary": "A car pulled in."}) == "A car pulled in.")
check("with nothing complete, the cut is tidied rather than left dangling",
      bridge._story_summary({"shortSummary": CUT}).endswith("instead\u2026")
      or bridge._story_summary({"shortSummary": CUT}).endswith("\u2026"))
check("tidying never returns empty",
      bridge._story_summary({"shortSummary": "Word"}) != "")
check("no metadata yields no summary",
      bridge._story_summary({}) == "")

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
