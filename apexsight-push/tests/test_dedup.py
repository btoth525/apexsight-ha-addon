"""The duplicate-review check must never let a FINAL follow-up move its clock.

Run:  PYTHONPATH=. python3 tests/test_dedup.py

Frigate often splits one activity into two reviews with the same objects, so /v1/notify drops a
second review's alert inside a 300 s window. Finals used to be silent, which kept them out of that
check. Once the AI story reached the phones, a level >= 1 final went out NON-silent and entered it:
it passed (same review id) but re-stamped the clock with its own time, ~105 s after the alert, so a
distinct visit 380 s after the first alert was dropped as a "duplicate" 275 s after the final.

Rules pinned here:
  - a final is never compared and never writes the state;
  - a silent final whose own alert was suppressed is dropped (nothing on the phone to replace);
  - a non-silent (AI-escalated) final always goes out.
"""
import os
import tempfile

os.environ.setdefault("APEX_DATA_DIR", tempfile.mkdtemp(prefix="apextest_dedup_"))
os.environ.setdefault("APEX_SECRET_KEY", "test")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")

from app import main  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


W = 300.0
PERSON = ["person"]
decide = main._dedup_decision

# ---- 1. The measured case: alert at 0, level-1 final at 105, distinct visit at 380 ----
supp, state = decide({}, "A", PERSON, "alert", False, 0.0, W)
check("first alert is delivered", supp is False)
check("and stamps the clock at its own time", state and state["ts"] == 0.0 and state["review_id"] == "A")
prev = state

supp, state = decide(prev, "A", PERSON, "final", False, 105.0, W)
check("its non-silent final is delivered", supp is False)
check("and writes nothing (the clock stays on the alert)", state is None)

supp, state = decide(prev, "B", PERSON, "alert", False, 380.0, W)
check("a distinct visit 380 s after the first alert is delivered", supp is False)
check("and becomes the new reference", state and state["review_id"] == "B" and state["ts"] == 380.0)

# ---- 2. A real duplicate is still suppressed, and remembered ----
supp, state = decide(prev, "B", PERSON, "alert", False, 100.0, W)
check("an identical second review inside the window is suppressed", supp is True)
check("the delivered alert's clock is untouched", state["ts"] == 0.0 and state["review_id"] == "A")
check("the suppressed review is remembered", "B" in state["suppressed"])
prev_b = state

supp, state = decide(prev_b, "B", PERSON, "final", True, 160.0, W)
check("its silent final is dropped too (no alert on the phone to replace)", supp is True)
check("without writing", state is None)

supp, state = decide(prev_b, "B", PERSON, "final", False, 160.0, W)
check("but an AI-escalated (non-silent) final still goes out", supp is False and state is None)

supp, _ = decide(prev_b, "A", PERSON, "final", True, 160.0, W)
check("the delivered review's silent final goes out", supp is False)

# ---- 3. A final can't be suppressed by label growth ----
supp, _ = decide(prev, "C", PERSON + ["car"], "final", False, 50.0, W)
check("a final is never compared on labels", supp is False)

# ---- 4. Fail-open cases for alerts ----
check("a same-review re-POST is delivered", decide(prev, "A", PERSON, "alert", False, 30.0, W)[0] is False)
check("a new object class is delivered", decide(prev, "B", ["car"], "alert", False, 30.0, W)[0] is False)
check("a gap past the window is delivered", decide(prev, "B", PERSON, "alert", False, 301.0, W)[0] is False)

# ---- 5. The remembered list survives a new reference and stays bounded ----
_, carried = decide(prev_b, "D", ["dog"], "alert", False, 200.0, W)
check("a new delivered alert carries the suppressed ids forward", "B" in carried["suppressed"])
state = {"ts": 0.0, "review_id": "A", "labels": PERSON, "suppressed": []}
for i in range(40):
    _, state = decide(state, f"R{i}", PERSON, "alert", False, 1.0, W)
check("the suppressed list is bounded", len(state["suppressed"]) == main._DEDUP_SUPPRESSED_KEEP)
check("and keeps the newest", state["suppressed"][-1] == "R39")

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
