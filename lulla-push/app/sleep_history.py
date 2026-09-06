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
                  poll_seconds: float = POLL_SECONDS,
                  until: Optional[float] = None) -> Iterable[tuple[float, Optional[str]]]:
    """Expand HA's change-only history into the regular sample stream the live poller sees.

    This is load-bearing, and getting it wrong is silent. HA's recorder stores a row only when a
    state CHANGES, so a state that held for 34 minutes is ONE row. `debounce()` confirms by
    seeing the same reading again after the hold has elapsed — with change-only input it never
    gets that second look, so nothing ever confirms, and a whole fortnight collapses into one
    22-hour "sleep" with zero wakings. (It did, before this existed.)
    """
    for i, (ts, state) in enumerate(readings):
        # HA's history is change-only: the LAST row is the current state, held until "now". Expand
        # it up to `until` (not just one sample) so the backfill reaches the present and connects
        # to the session the live poller is currently extending, instead of leaving a gap.
        if i + 1 < len(readings):
            end = readings[i + 1][0]
        else:
            end = max(until, ts + poll_seconds) if until else ts + poll_seconds
        t = ts
        while t < end:
            yield t, state
            t += poll_seconds


def segments_from_readings(readings: Iterable[tuple[float, Optional[str]]],
                           *, wake_hold: float = owlet_log.WAKE_HOLD_SECONDS,
                           stage_hold: float = owlet_log.STAGE_HOLD_SECONDS,
                           already_gridded: bool = False,
                           until: Optional[float] = None) -> list[Segment]:
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
    stream = rows if already_gridded else list(_on_poll_grid(rows, until=until))

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


# ---- Owlet-matched sessions (the numbers Taylor compares against) ------------------------
#
# The debounced segments above are right for the notification-free chart bands, but Owlet's
# Sleep Summary (Time asleep / Wakings / Awake·Light·Deep) is computed at 1-MINUTE resolution
# over a whole SOCK SESSION, and that's what the family sees. Reverse-engineered against a real
# Owlet screenshot (night of 8:26 PM→7:18 AM): matching Owlet needs three things the debounce
# path got wrong —
#   1. 1-minute binning (Owlet "updates every minute"), not a 5-minute hold,
#   2. one session across the whole sock-on period (brief sock-off bridged), not fragmented naps,
#   3. a waking = a SUSTAINED wake, not every stir.
# With those, the durations matched to within a minute (deep was exact) and the waking rule below
# reproduced Owlet's count of 8.

# Sock-off (nosignal) shorter than this is bridged INSIDE a session — a feed/change/adjust, not
# the end of the night. Longer ends the session. (Owlet bridged the brief gaps in the sample.)
SESSION_BRIDGE_MINUTES = 15
# Waking smoothing, calibrated to Owlet's count: a wake registers only after this many continuous
# awake minutes, and can't register again until this many continuous asleep minutes have passed.
WAKE_REGISTER_MINUTES = 5
WAKE_REARM_MINUTES = 10
# A session is worth showing once it holds at least this much actual sleep.
MIN_SESSION_ASLEEP_MINUTES = 10

_ASLEEP_STATES = {"light_sleep", "deep_sleep"}
_NOSIGNAL_STATES = {"", "nosignal", "unavailable", "unknown", "not placed", "none", "off"}


def _minute_state(raw: Optional[str]) -> str:
    """Normalize a stored/HA sleep_state to one of light_sleep|deep_sleep|awake|nosignal."""
    if raw is None:
        return "nosignal"
    s = raw.strip().lower()
    if s in _ASLEEP_STATES:
        return s
    if s in _NOSIGNAL_STATES:
        return "nosignal"
    if s == "awake":
        return "awake"
    return "awake" if "wake" in s else ("light_sleep" if "sleep" in s else "nosignal")


def _fill_minutes(minutes: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Expand a sparse (minute_ts, state) list to a dense per-minute grid, carrying the last state
    across gaps up to the bridge (feed/change) and marking longer gaps as nosignal."""
    if not minutes:
        return []
    bridge = SESSION_BRIDGE_MINUTES
    out: list[tuple[int, str]] = []
    for i, (ts, raw) in enumerate(minutes):
        state = _minute_state(raw)
        out.append((ts, state))
        nxt = minutes[i + 1][0] if i + 1 < len(minutes) else ts + 60
        missing = int((nxt - ts) // 60) - 1
        if missing <= 0:
            continue
        # A short hole while the sock stays reporting the same non-nosignal state = carry it
        # (Owlet bridges brief gaps); a long hole = nosignal (sock genuinely off).
        fill = state if (state != "nosignal" and missing <= bridge) else "nosignal"
        for k in range(1, missing + 1):
            out.append((ts + k * 60, fill))
    return out


@dataclass
class OwletSession:
    start: float
    end: float
    asleep_minutes: int
    light_minutes: int
    deep_minutes: int
    awake_minutes: int
    wakings: int
    longest_stretch_minutes: int
    segments: list[Segment]

    def as_dict(self) -> dict:
        return {
            "start": owlet_log.iso_at(self.start),
            "end": owlet_log.iso_at(self.end),
            "asleep_seconds": self.asleep_minutes * 60,
            "light_seconds": self.light_minutes * 60,
            "deep_seconds": self.deep_minutes * 60,
            "awake_seconds": self.awake_minutes * 60,
            "wakings": self.wakings,
            "longest_stretch_seconds": self.longest_stretch_minutes * 60,
            "segments": [s.as_dict() for s in self.segments],
        }


def _count_wakings(states: list[str]) -> int:
    """Owlet-style waking count over one session's per-minute states. A waking is a SUSTAINED
    awakening between sleep bouts: it registers after WAKE_REGISTER_MINUTES continuous awake, and
    won't register another until WAKE_REARM_MINUTES continuous asleep have re-armed it. Leading
    (settling) and trailing (final wake) awake are excluded by only scanning between the first and
    last asleep minute."""
    idx = [i for i, s in enumerate(states) if s in _ASLEEP_STATES]
    if not idx:
        return 0
    core = states[idx[0]: idx[-1] + 1]
    wakings = 0
    armed = True
    awake_run = 0
    asleep_run = 0
    for s in core:
        if s == "awake":
            awake_run += 1
            asleep_run = 0
            if armed and awake_run >= WAKE_REGISTER_MINUTES:
                wakings += 1
                armed = False
        elif s in _ASLEEP_STATES:
            asleep_run += 1
            awake_run = 0
            if asleep_run >= WAKE_REARM_MINUTES:
                armed = True
        else:  # a bridged nosignal minute — neither confirms nor breaks a wake
            awake_run = 0
    return wakings


def owlet_sessions(minutes: list[tuple[int, str]]) -> list[OwletSession]:
    """Build Owlet-matched sessions from a per-minute timeline. One session per sock-on period
    (brief sock-off bridged); stats at 1-minute resolution to match Owlet's Sleep Summary."""
    dense = _fill_minutes(minutes)
    if not dense:
        return []
    # Split into sock-on runs separated by a real (unbridged) nosignal stretch.
    groups: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    nosig_run = 0
    for ts, s in dense:
        if s == "nosignal":
            nosig_run += 1
            if nosig_run > SESSION_BRIDGE_MINUTES:
                if cur:
                    groups.append(cur)
                    cur = []
                continue
        else:
            nosig_run = 0
        cur.append((ts, s))
    if cur:
        groups.append(cur)

    sessions: list[OwletSession] = []
    for g in groups:
        # Trim leading/trailing nosignal but KEEP leading/trailing awake (Owlet's span includes
        # settling and the final wake).
        while g and g[0][1] == "nosignal":
            g.pop(0)
        while g and g[-1][1] == "nosignal":
            g.pop()
        if not g:
            continue
        states = [s for _, s in g]
        if not any(s in _ASLEEP_STATES for s in states):
            continue
        light = states.count("light_sleep")
        deep = states.count("deep_sleep")
        asleep = light + deep
        if asleep < MIN_SESSION_ASLEEP_MINUTES:
            continue
        awake = states.count("awake")
        # longest unbroken asleep run (minutes)
        longest = best = 0
        for s in states:
            if s in _ASLEEP_STATES:
                best += 1
                longest = max(longest, best)
            else:
                best = 0
        # hypnogram bands = runs of the per-minute state (so the chart shows Owlet's fine detail)
        segments: list[Segment] = []
        run_state = states[0]
        run_start = g[0][0]
        for (ts, s) in g[1:] + [(g[-1][0] + 60, None)]:
            if s != run_state:
                if run_state != "nosignal":
                    segments.append(Segment(run_state, float(run_start), float(ts)))
                run_state = s
                run_start = ts
        sessions.append(OwletSession(
            start=float(g[0][0]), end=float(g[-1][0] + 60),
            asleep_minutes=asleep, light_minutes=light, deep_minutes=deep, awake_minutes=awake,
            wakings=_count_wakings(states), longest_stretch_minutes=longest, segments=segments))
    return sessions
