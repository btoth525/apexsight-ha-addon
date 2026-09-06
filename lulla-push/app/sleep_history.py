"""Sleep segments and sessions — the data behind the hypnogram (the chart Taylor likes in the
Owlet app), assembled here so every surface tells the same story.

Why this lives on the relay and not in the app:

  * **Retention.** HA's recorder keeps ~10 days. Owlet keeps session history indefinitely. If we
    read the recorder on demand, Taylor gets a beautiful chart for last night and an empty one
    for last month, with nothing to explain it. So the relay WRITES segments as they close and
    keeps them; the recorder is used once, to backfill the days we still have.
  * **One source of truth.** The app must not re-derive bands from the raw `sleep_state`, or the
    chart would strobe and count ~70 wakings for a night the sleep log correctly records as ~8.
    Segmentation runs off the same DEBOUNCED signal as the notifications and the auto-log.

Everything here is pure except the callers in main.py — segment maths is unit-tested with no
clock, network, or DB.

**Information only.** Nothing here grades a night. Owlet's own "Sleep Quality Indicators" are
raw metrics, not a composite score, and a Lulla sleep score would collide head-on with the rule
that nothing derives judgment from vitals and that copy never implies the baby is behind. We
show the numbers; the parent decides what they mean.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from . import owlet_log

# A band is what one horizontal stripe of the hypnogram is made of.
AWAKE = "awake"
LIGHT = "light_sleep"
DEEP = "deep_sleep"

# An awake stretch at least this long ENDS a session rather than counting as a waking inside it.
# Below it, she stirred and went back down — which is what a parent means by "she woke twice",
# not "she had three separate naps".
SESSION_GAP_SECONDS = 1200.0    # 20 minutes

# Segments shorter than this are dropped when assembling a session — they're the tail of the
# debounce, not something that happened.
MIN_BAND_SECONDS = 30.0


def band_for(sleep_class: str, stage: Optional[str]) -> Optional[str]:
    """The band to draw, from the two CONFIRMED signals. Returns None for "don't draw anything"
    (sock off / charging / no reading) — a gap in the chart, never a zero.

    The class signal (5-minute hold) decides asleep-vs-awake so the chart, the sleep log, and the
    notifications can never disagree about a waking. The stage signal (2-minute hold) only picks
    light-vs-deep INSIDE a sleep. When the sock says "awake" while the class still says asleep,
    that's a stir mid-nap: it draws as light sleep, and it does not count as a waking.
    """
    if sleep_class == "nosignal":
        return None
    if sleep_class == AWAKE:
        return AWAKE
    if stage and owlet_log.sleep_class(stage) == "asleep":
        return stage                 # light_sleep / deep_sleep, straight from the sock
    return LIGHT


@dataclass
class Segment:
    """One band: a stage that held from `start` to `end` (epoch seconds)."""
    band: str
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict:
        return {"band": self.band,
                "start": owlet_log.iso_at(self.start),
                "end": owlet_log.iso_at(self.end),
                "seconds": round(self.seconds)}


# The live poller's cadence. The backfill replays onto this same grid — see below for why that
# is not optional.
POLL_SECONDS = 15.0


def _on_poll_grid(readings: list[tuple[float, Optional[str]]],
                  poll_seconds: float = POLL_SECONDS) -> Iterable[tuple[float, Optional[str]]]:
    """Expand HA's change-only history into the regular sample stream the live poller sees.

    This is load-bearing, and getting it wrong is silent. HA's recorder stores a row only when a
    state CHANGES, so a state that held for 34 minutes is ONE row. `debounce()` confirms by
    seeing the same reading again after the hold has elapsed — with change-only input it never
    gets that second look, so nothing ever confirms, and a whole fortnight collapses into one
    22-hour "sleep" with zero wakings. (It did, before this existed.)
    """
    for i, (ts, state) in enumerate(readings):
        end = readings[i + 1][0] if i + 1 < len(readings) else ts + poll_seconds
        t = ts
        while t < end:
            yield t, state
            t += poll_seconds


def segments_from_readings(readings: Iterable[tuple[float, Optional[str]]],
                           *, wake_hold: float = owlet_log.WAKE_HOLD_SECONDS,
                           stage_hold: float = owlet_log.STAGE_HOLD_SECONDS,
                           already_gridded: bool = False) -> list[Segment]:
    """Replay raw `(timestamp, sleep_state)` samples through the SAME debounce the live poller
    uses, and return closed bands.

    Used for the one-time backfill out of HA's recorder, and it's what makes the backfilled days
    identical in shape to the days the poller writes live — a seam there would show up as a
    chart that changes character ten days back. Pass `already_gridded=True` for input that is
    already a regular sample stream (tests); anything out of the recorder needs the expansion.
    """
    rows = list(readings)
    if not rows:
        return []
    stream = rows if already_gridded else list(_on_poll_grid(rows))

    cls_state = owlet_log.Debounced()
    stage_state = owlet_log.Debounced()
    out: list[Segment] = []
    current: Optional[str] = None
    started = 0.0
    ts = stream[0][0]

    for ts, raw in stream:
        stage_reading = raw if raw and owlet_log.sleep_class(raw) != "nosignal" else None
        stage_state, _ = owlet_log.debounce(stage_state, stage_reading, ts, stage_hold)
        cls_state, _ = owlet_log.debounce(cls_state, owlet_log.sleep_class(raw), ts, wake_hold)
        band = band_for(cls_state.confirmed or "nosignal", stage_state.confirmed)
        if band == current:
            continue
        if current is not None:
            out.append(Segment(current, started, ts))
        current, started = band, ts
    if current is not None:
        out.append(Segment(current, started, ts))
    return [s for s in out if s.band and s.seconds > 0]


@dataclass
class Session:
    """A night or a nap: one stretch of sleep, brief wakings included."""
    start: float
    end: float
    asleep_seconds: float
    deep_seconds: float
    light_seconds: float
    awake_seconds: float          # time awake WITHIN the session (the wakings)
    wakings: int
    longest_stretch: float        # longest unbroken asleep run
    segments: list[Segment]

    def as_dict(self) -> dict:
        return {
            "start": owlet_log.iso_at(self.start),
            "end": owlet_log.iso_at(self.end),
            "asleep_seconds": round(self.asleep_seconds),
            "deep_seconds": round(self.deep_seconds),
            "light_seconds": round(self.light_seconds),
            "awake_seconds": round(self.awake_seconds),
            "wakings": self.wakings,
            "longest_stretch_seconds": round(self.longest_stretch),
            "segments": [s.as_dict() for s in self.segments],
        }


def sessions_from_segments(segments: list[Segment],
                           *, gap_seconds: float = SESSION_GAP_SECONDS,
                           min_band: float = MIN_BAND_SECONDS) -> list[Session]:
    """Group bands into the sessions a parent would recognise.

    A session runs from the first asleep band until an awake stretch of `gap_seconds` or more (or
    a gap in the data) closes it. Shorter awake stretches stay INSIDE and count as wakings —
    matching how Owlet defines a waking (asleep → awake → asleep within one session), which is
    also how a parent counts them.
    """
    usable = [s for s in segments if s.seconds >= min_band]
    sessions: list[Session] = []
    run: list[Segment] = []

    def close() -> None:
        # Trim trailing awake so a session ends when she woke, not when we noticed.
        while run and run[-1].band == AWAKE:
            run.pop()
        if not run:
            return
        asleep = [s for s in run if s.band != AWAKE]
        if not asleep:
            run.clear()
            return
        # The longest unbroken asleep run — the number every parent of a newborn actually wants.
        longest = best = 0.0
        for s in run:
            if s.band == AWAKE:
                best = 0.0
            else:
                best += s.seconds
                longest = max(longest, best)
        sessions.append(Session(
            start=run[0].start, end=run[-1].end,
            asleep_seconds=sum(s.seconds for s in asleep),
            deep_seconds=sum(s.seconds for s in run if s.band == DEEP),
            light_seconds=sum(s.seconds for s in run if s.band == LIGHT),
            awake_seconds=sum(s.seconds for s in run if s.band == AWAKE),
            wakings=sum(1 for s in run if s.band == AWAKE),
            longest_stretch=longest, segments=list(run)))
        run.clear()

    previous_end: Optional[float] = None
    for seg in usable:
        # A hole in the data (sock off, charging) always ends a session — we can't claim she was
        # asleep through a stretch we weren't watching.
        if previous_end is not None and seg.start - previous_end > min_band:
            close()
        previous_end = seg.end
        if seg.band == AWAKE and seg.seconds >= gap_seconds:
            close()
            continue
        if not run and seg.band == AWAKE:
            continue                  # a session starts when she falls asleep, not before
        run.append(seg)
    close()
    return sessions
