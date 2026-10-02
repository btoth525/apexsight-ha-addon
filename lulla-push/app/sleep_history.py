"""Sleep segments and sessions — the data behind the hypnogram (the chart Taylor likes in the
Owlet app), assembled here so every surface tells the same story.

Why this lives on the relay and not in the app:

  * **Retention.** HA's recorder keeps ~10 days. Owlet keeps session history indefinitely. If we
    read the recorder on demand, Taylor gets a beautiful chart for last night and an empty one
    for last month, with nothing to explain it. So the relay WRITES the sock's state once a
    minute (`sleep_minute`) and keeps it; the recorder is used once, to backfill the days we
    still have.
  * **One source of truth.** The app must not re-derive bands from the raw `sleep_state`; it
    draws what the relay hands it.

Two paths live here, and only ONE of them feeds the app today:

  * `owlet_sessions()` (bottom of the file) — the per-MINUTE, Owlet-matched sessions. This is
    what `/v1/home/sleep/sessions` (the hypnogram + session card) and the `owlet.refresh` push's
    widget summary are built from.
  * `segments_from_readings()` / `sessions_from_segments()` — the DEBOUNCED bands. The poller
    and the backfill still write them to the `sleep_segments` table, but no endpoint reads that
    table any more; it's kept as an audit trail of the debounced signal (the one that drives the
    notifications and the auto-log), and these functions stay unit-tested for that reason.

Everything here is pure except the callers in main.py — segment maths is unit-tested with no
clock, network, or DB.

**Information only.** Nothing here grades a night. Owlet's own "Sleep Quality Indicators" are
raw metrics, not a composite score, and a Lulla sleep score would collide head-on with the rule
that nothing derives judgment from vitals and that copy never implies the baby is behind. We
show the numbers; the parent decides what they mean.
"""
from __future__ import annotations

import heapq
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

# Sock-off (nosignal) shorter than this is bridged INSIDE a sock-on run — a feed/change/adjust,
# not the end of the night. (Owlet bridged the brief gaps in the sample.) It is also how long a
# hole with NO rows at all is carried forward as the last state (see `_fill_minutes`).
SESSION_BRIDGE_MINUTES = 15
# Two sessions separated by at most this much no-data (sock off) are ONE night. A 3 AM feed
# with the sock on the charger for 19 minutes split Sun 9/27 into "9:10 PM–12:11 AM" and
# "12:30–9:16 AM"; no parent would call that two sleeps. The gap itself stays out of every
# total and is left UNCOVERED between bands (the app has no "no data" band kind), never drawn
# as sleep.
NIGHT_MERGE_GAP_MINUTES = 45
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
    won't register another until WAKE_REARM_MINUTES asleep minutes (uninterrupted by awake; a
    nosignal minute pauses the count rather than resetting it) have re-armed it. Leading
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
        else:
            # A nosignal minute inside the session (sock off / no reading). We can't see her, so
            # it never counts AS awake or asleep. It does break an in-progress awake run — a
            # waking needs WAKE_REGISTER_MINUTES of continuously OBSERVED awake, so awake minutes
            # either side of a sock-off don't add up — but it leaves `asleep_run` and `armed`
            # alone, so sleep either side of a sock-off still counts toward re-arming.
            awake_run = 0
    return wakings


def _sock_on_runs(dense: list[tuple[int, str]]) -> list[tuple[int, int]]:
    """Index ranges `[i0, i1]` (inclusive) into `dense` of the sock-on runs: stretches separated
    by MORE than SESSION_BRIDGE_MINUTES of nosignal, trimmed of nosignal at both ends. Leading /
    trailing AWAKE is kept (Owlet's span includes settling and the final wake)."""
    runs: list[tuple[int, int]] = []
    first: Optional[int] = None       # first real (non-nosignal) index of the open run
    last: Optional[int] = None        # last real index of the open run
    nosig_run = 0
    for i, (_ts, s) in enumerate(dense):
        if s == "nosignal":
            nosig_run += 1
            if nosig_run > SESSION_BRIDGE_MINUTES and first is not None:
                runs.append((first, last))
                first = last = None
            continue
        nosig_run = 0
        if first is None:
            first = i
        last = i
    if first is not None:
        runs.append((first, last))
    return runs


def _session_from(g: list[tuple[int, str]]) -> OwletSession:
    """Stats + bands for one night's dense minutes (starts and ends on a real minute). Every
    total counts REAL minutes only — a nosignal minute is never asleep, awake, or a waking — and
    nosignal runs are left as uncovered time between bands rather than drawn as anything."""
    states = [s for _, s in g]
    light = states.count("light_sleep")
    deep = states.count("deep_sleep")
    awake = states.count("awake")
    # Longest unbroken asleep run (minutes). Awake OR a sock-off breaks it: we can't claim she
    # slept through a stretch we weren't watching.
    longest = best = 0
    for s in states:
        if s in _ASLEEP_STATES:
            best += 1
            longest = max(longest, best)
        else:
            best = 0
    # Hypnogram bands = runs of the per-minute state (so the chart shows Owlet's fine detail).
    segments: list[Segment] = []
    run_state = states[0]
    run_start = g[0][0]
    for (ts, s) in g[1:] + [(g[-1][0] + 60, None)]:
        if s != run_state:
            if run_state != "nosignal":
                segments.append(Segment(run_state, float(run_start), float(ts)))
            run_state = s
            run_start = ts
    return OwletSession(
        start=float(g[0][0]), end=float(g[-1][0] + 60),
        asleep_minutes=light + deep, light_minutes=light, deep_minutes=deep, awake_minutes=awake,
        wakings=_count_wakings(states), longest_stretch_minutes=longest, segments=segments)


def owlet_sessions(minutes: list[tuple[int, str]],
                   *, merge_gap_minutes: int = NIGHT_MERGE_GAP_MINUTES) -> list[OwletSession]:
    """Build Owlet-matched sessions from a per-minute timeline, oldest first. One session per
    sock-on period (brief sock-off bridged); stats at 1-minute resolution to match Owlet's Sleep
    Summary.

    Sessions separated by at most `merge_gap_minutes` (end of one to start of the next) are then
    merged into one night. Whatever lies between them stays as it was: real minutes (a short
    awake run, a fragment too small to be its own session) count in the totals like any other,
    and the sock-off minutes count in nothing and draw as a hole between bands."""
    dense = _fill_minutes(minutes)
    if not dense:
        return []

    def asleep_in(r: tuple[int, int]) -> int:
        return sum(1 for _, s in dense[r[0]: r[1] + 1] if s in _ASLEEP_STATES)

    # A run is a session once it holds enough actual sleep (same bar as before the merge).
    runs = [r for r in _sock_on_runs(dense) if asleep_in(r) >= MIN_SESSION_ASLEEP_MINUTES]

    nights: list[tuple[int, int]] = []
    for r in runs:
        if nights:
            prev_end = dense[nights[-1][1]][0] + 60          # end of the previous session
            if dense[r[0]][0] - prev_end <= merge_gap_minutes * 60:
                nights[-1] = (nights[-1][0], r[1])
                continue
        nights.append(r)
    return [_session_from(dense[i0: i1 + 1]) for i0, i1 in nights]


# ---- Band merging (the widget summary has to fit in one push) ------------------------------
#
# The `owlet.refresh` push carries the newest night's bands for the home-screen barcode, and
# APNs refuses ANY payload over 4 KB — the whole push, owlet reading included. A busy night is
# ~95 one-minute-resolution runs; with default JSON separators that was 4.1–4.4 KB (measured on
# 9/21, 9/23, 9/25), so the widget stopped updating near wake-up. The old cap was stride sampling
# (keep every Nth band), which SKIPS bands, so the barcode under-fills and can lose a waking.
# Merging instead keeps every second covered.

# An awake stretch this long is a real waking (WAKE_REGISTER_MINUTES); merging must never hide one
# inside a sleep-coloured band.
PROTECT_AWAKE_SECONDS = WAKE_REGISTER_MINUTES * 60
_KIND_TIE_ORDER = {AWAKE: 0, LIGHT: 1, DEEP: 2}     # on a tie, awake wins (never hide a waking)


@dataclass
class _BandGroup:
    start: float
    end: float
    seconds: dict                     # band kind -> real (covered) seconds, gaps excluded

    @property
    def kind(self) -> str:
        return min(self.seconds, key=lambda k: (-self.seconds[k], _KIND_TIE_ORDER.get(k, 9)))

    def protected(self, protect_awake_seconds: float) -> bool:
        """An awake band holding a real waking's worth of awake — never to be painted as sleep.
        Judged on the GROUP, so an awake band grown out of merges is protected too."""
        return self.kind == AWAKE and self.seconds.get(AWAKE, 0.0) >= protect_awake_seconds


def _combine(a: _BandGroup, b: _BandGroup,
             protect_awake_seconds: float) -> Optional[_BandGroup]:
    """`a` and `b` as one band, or None if that would swallow a protected waking into sleep."""
    secs = dict(a.seconds)
    for k, v in b.seconds.items():
        secs[k] = secs.get(k, 0.0) + v
    merged = _BandGroup(start=a.start, end=b.end, seconds=secs)
    if merged.kind != AWAKE and (a.protected(protect_awake_seconds)
                                 or b.protected(protect_awake_seconds)):
        return None
    return merged


def merge_bands(segments: list[Segment], max_bands: int,
                *, protect_awake_seconds: float = PROTECT_AWAKE_SECONDS) -> list[Segment]:
    """Reduce `segments` to at most `max_bands` by merging ADJACENT bands — never dropping one —
    so the merged list still starts where the first band started and ends where the last ended.

    Greedy: always merge the adjacent pair whose combined span is shortest (flicker first; a
    sock-off gap between two bands counts in the span, so bands either side of a gap merge last).
    A merged band takes the kind with the most real seconds in it (ties → awake). An awake band
    of `protect_awake_seconds` or more is never merged into a sleep-coloured band; it may only
    absorb neighbours while staying awake. If that rule leaves no legal merge, the result can be
    longer than `max_bands` — the caller decides what to do then."""
    if max_bands < 1 or len(segments) <= max_bands:
        return list(segments)

    groups: list[Optional[_BandGroup]] = [
        _BandGroup(start=s.start, end=s.end, seconds={s.band: s.seconds}) for s in segments]
    n = len(groups)
    nxt = list(range(1, n + 1))
    prv = list(range(-1, n - 1))
    version = [0] * n
    heap: list = []

    def push_pair(i: int) -> None:
        if i < 0 or nxt[i] >= n:
            return
        j = nxt[i]
        merged = _combine(groups[i], groups[j], protect_awake_seconds)
        if merged is not None:
            heapq.heappush(heap, (merged.end - merged.start, groups[i].start, i, version[i],
                                  j, version[j]))

    for i in range(n - 1):
        push_pair(i)
    count = n
    while count > max_bands and heap:
        _span, _start, i, vi, j, vj = heapq.heappop(heap)
        if (groups[i] is None or groups[j] is None or version[i] != vi or version[j] != vj
                or nxt[i] != j):
            continue                  # stale: one side has merged since this was queued
        groups[i] = _combine(groups[i], groups[j], protect_awake_seconds)
        groups[j] = None
        version[i] += 1
        nxt[i] = nxt[j]
        if nxt[j] < n:
            prv[nxt[j]] = i
        count -= 1
        if prv[i] >= 0:
            push_pair(prv[i])
        push_pair(i)
    return [Segment(g.kind, g.start, g.end) for g in groups if g is not None]
