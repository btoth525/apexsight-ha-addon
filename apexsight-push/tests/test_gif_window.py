"""Regression tests for the follow-up push's GIF window.

Run:  PYTHONPATH=. python3 tests/test_gif_window.py

The GIF used to span the whole review. On a verified 95-second doorbell review that is 780 KB,
against ~200 KB for 20 seconds — and the notification service extension has a bounded window to
download it, so on cellular the difference decides whether an image appears at all.

Measured honestly: across 12 real reviews this saves only ~5% overall, because most reviews are
already under the cap. It is a WORST-CASE GUARD for the long tail, not a general win.

The load-bearing invariant is that truncation must never cut off the moment the alert is about.
Head-truncating that same doorbell review would have shown an empty porch — the delivery happened
~70s in — which is why the window is centered on the event's best frame rather than the start.
"""
import os

os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
import bridge  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


# The real doorbell review this was diagnosed against.
RS, RE = 1785110287.546, 1785110382.937      # 95 seconds
BEST = 1785110357.548                        # verified: the frame showing the delivery, ~70s in

gs, ge = bridge._gif_window(BEST, RS, RE)
check("long review is capped near the span", 19 <= ge - gs <= 21)
check("window stays inside the review", gs >= int(RS) and ge <= RE + 1)
check("window COVERS the key moment (head-truncating would miss it)", gs <= BEST <= ge)
check("window is not the whole review", (ge - gs) < (RE - RS))

# A short review must be left completely alone.
gs, ge = bridge._gif_window(RS + 4, RS, RS + 8)
check("short review keeps its full window", gs == int(RS) and ge >= RS + 8)

# Unknown best frame → fall back to the whole review rather than guessing which slice matters.
gs, ge = bridge._gif_window(None, RS, RE)
check("unknown best frame falls back to the full review window",
      gs == int(RS) and ge >= RE)

# The moment is covered wherever it sits — start, middle, or the final fractional second.
for label, best in (("at start", RS), ("mid-review", (RS + RE) / 2), ("at end", RE)):
    gs, ge = bridge._gif_window(best, RS, RE)
    check(f"covers a best frame {label}", gs <= best <= ge)
    check(f"stays in bounds with best frame {label}", gs >= int(RS) and ge <= RE + 1)

# The end is rounded UP: truncating it dropped up to a second, which could cut off a best frame
# landing in that final fraction (this exact case failed before the ceil was added).
gs, ge = bridge._gif_window(RE, RS, RE)
check("end is rounded up so the final fractional second isn't dropped", ge >= RE)

# The final push's centre: an unreadable event (401 / 404 / timeout) must NOT leave the GIF
# unbounded — the relay now ships the bridge's GIF as-is, so the review's thumb_time stands in.
THUMB = RS + 60
check("centre uses the event's best frame when it was read",
      bridge._gif_centre(BEST, THUMB, RS, RE) == BEST)
check("centre falls back to the review's thumb_time when the event is unreadable",
      bridge._gif_centre(None, THUMB, RS, RE) == THUMB)
gs, ge = bridge._gif_window(bridge._gif_centre(None, THUMB, RS, RE), RS, RE)
check("an unreadable event still gets a bounded GIF", 19 <= ge - gs <= 21)
check("that GIF covers the thumbnail moment", gs <= THUMB <= ge)
check("a thumb_time outside the review is clamped into it",
      bridge._gif_centre(None, RE + 500, RS, RE) == RE)
check("no best frame and no thumb_time → no centre (full-window fallback)",
      bridge._gif_centre(None, None, RS, RE) is None)

# Degenerate inputs must not produce an inverted or negative window.
gs, ge = bridge._gif_window(RS, RS, RS)
check("zero-length review yields a non-inverted window", ge >= gs)

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
