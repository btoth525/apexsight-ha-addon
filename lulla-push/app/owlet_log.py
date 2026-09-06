"""Owlet → automatic sleep logging (pure logic + LogEvent payload builder).

The relay is the SINGLE writer for auto-logged sleep, so both phones pull ONE shared entry
(never a duplicate-per-phone). Everything here is a pure function of its inputs — unit-tested
without a clock, network, or DB. `main.py`'s background poller does the I/O (fetch HA, persist
the open-session cursor, write the record via db.upsert).

Design choices that matter:
  * CLOSE-only writes. We record a sleep event when the baby WAKES (start + end both known and
    the nap was long enough), not while she's still asleep. That avoids ever writing an
    in-progress event we'd later have to discard/tombstone, and matches how a parent logs sleep
    (after the fact).
  * Deterministic id (uuid5 of the session start). Idempotent: re-running a tick can't create a
    second event for the same session.
  * source='owlet' + createdBy='owlet' so the app shows/filters it as auto-logged, and childID
    is null (the app treats null as "any child", so it shows under the one baby).

Sleep-state vocabulary is confirmed against the real sock the first time it's worn; the AWAKE /
NO-SIGNAL sets below are the tunable knobs.
"""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# uuid5 namespace (RFC-4122 example NS) — keeps a session's event id stable across ticks/restarts.
_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Explicitly-awake states. Everything else that's a real, available reading is treated as asleep.
_AWAKE = {"awake", "settling", "awake settling", "wide awake"}
# States meaning "the sock isn't reporting sleep" — ends an open session, never starts one.
_NO_SIGNAL = {"", "unavailable", "unknown", "not placed", "none", "off", "disconnected"}


def sleep_class(state: Optional[str]) -> str:
    """Map a raw HA sleep_state value to 'asleep' | 'awake' | 'nosignal'."""
    if state is None:
        return "nosignal"
    s = state.strip().lower()
    if s in _NO_SIGNAL:
        return "nosignal"
    if s in _AWAKE:
        return "awake"
    return "asleep"   # light / deep / asleep / rem / …


def sleep_class_from_alerts(alerts: dict, sleep_state: Optional[str]) -> str:
    """Prefer Owlet's own `awake` binary flag (cleaner than the text state); fall back to the
    sleep_state string. A raised `sock_off` flag means she isn't being monitored → 'nosignal'."""
    if alerts.get("sock_off") is True:
        return "nosignal"
    awake = alerts.get("awake")
    if awake is True:
        return "awake"
    if awake is False:
        return "asleep"
    return sleep_class(sleep_state)


# Owlet's own alert flags → (human phrase, safety-critical?). We RELAY these; the sock/base
# station raises them. Safety-critical ones push time-sensitive (pierce Sleep Focus).
ALERT_META = {
    "low_o2": ("low oxygen", True),
    "high_o2": ("high oxygen", True),
    "low_hr": ("low heart rate", True),
    "high_hr": ("high heart rate", True),
    "sock_off": ("the sock came off", True),
    "sock_disconnected": ("the sock disconnected", True),
    "lost_power": ("the base station lost power", False),
    "low_battery": ("the sock battery is low", False),
}


def stage_changed(prev: Optional[str], cur: Optional[str]) -> bool:
    """True when `cur` is a real, available sleep stage that differs from the last-seen one — the
    trigger for a (passive, rate-limited) 'she's now in deep sleep' notification. A missing/no-
    signal reading is never a stage change (charging/sock-off shouldn't ping)."""
    if not cur or sleep_class(cur) == "nosignal":
        return False
    return prev is not None and prev != cur


def stage_label(state: str) -> str:
    """Human label for a sleep-stage push, vocab-agnostic (whatever HA reports)."""
    return state.strip().replace("_", " ").title()


def alert_transitions(prev: dict, cur: dict) -> list[str]:
    """Alert keys that just went OFF→ON (edge-triggered, so we notify once per episode, not
    every poll). 'awake' is a sleep signal, never an alert."""
    fired = []
    for key, is_on in cur.items():
        if key == "awake" or not is_on:
            continue
        if not prev.get(key, False) and key in ALERT_META:
            fired.append(key)
    return fired


@dataclass
class SleepDecision:
    new_open_start: Optional[str]        # the open-session start to persist for the next tick
    write: Optional[dict]                # {"start": iso, "end": iso} to log now, or None


def decide(cur_class: str, open_start: Optional[str], now_iso: str,
           min_minutes: float = 5.0) -> SleepDecision:
    """Advance the sleep state machine one tick.

    - asleep, none open        -> start tracking (persist start, write nothing yet)
    - asleep, already open      -> keep sleeping (no change)
    - awake/nosignal, open      -> WAKE: log start..now IF it lasted >= min_minutes, else discard
    - awake/nosignal, none open -> nothing
    """
    if cur_class == "asleep":
        return SleepDecision(open_start or now_iso, None)
    if open_start is not None:
        minutes = (_parse(now_iso) - _parse(open_start)).total_seconds() / 60.0
        if minutes >= min_minutes:
            return SleepDecision(None, {"start": open_start, "end": now_iso})
        return SleepDecision(None, None)   # a <5-minute blip is not a nap — drop it
    return SleepDecision(None, None)


def session_event_id(start_iso: str) -> str:
    """Deterministic per session — and **UPPERCASE**, because Swift's `UUID.uuidString` is
    uppercase. The server's dedupe key is a case-sensitive string, so emitting Python's default
    lowercase made the app's round-tripped copy land as a SECOND record for the same sleep
    (harmless in-app, since Swift UUID equality ignores case, but it doubled storage and caused
    endless push/pull churn). Matching Swift's casing keeps one row per session."""
    return str(uuid.uuid5(_NS, f"owlet-sleep-{start_iso}")).upper()


def build_sleep_payload(*, start_iso: str, end_iso: str, tz: str, now_iso: str) -> dict:
    """A LogEventSnapshot the iOS app decodes as a completed sleep. Only the fields the app's
    decoder treats as required are set; every other field is optional and omitted (decodes nil).
    """
    return {
        "id": session_event_id(start_iso),
        "kindRaw": "sleep",
        "startAt": start_iso,
        "endAt": end_iso,
        "timezoneID": tz,
        "sourceRaw": "owlet",
        "childID": None,
        "diaperBlowout": False,
        "diaperRash": False,
        "createdBy": "owlet",
        "createdByRole": None,
        "createdAt": start_iso,
        "updatedAt": now_iso,
        "isTombstoned": False,
    }


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deep_arm_decision(*, armed_until: float, now: float, prev_stage: Optional[str],
                      cur_stage: Optional[str], sleep_class_confirmed: str) -> str:
    """Pure decision for the one-shot "tell me at deep sleep" alert. Returns:
      * "fire"   — a fresh deep-sleep entry while armed and confirmed-asleep → alert + disarm,
      * "expire" — the arm window has passed → clear it,
      * "hold"   — nothing to do.

    Fires on the EDGE (prev != deep, cur == deep), so being deep for a while can't re-fire, and
    only while the debounced class says asleep, so a raw flicker around a wake can't trip it.
    """
    if armed_until and now > armed_until:
        return "expire"
    if (armed_until and now <= armed_until
            and cur_stage == "deep_sleep" and prev_stage != "deep_sleep"
            and sleep_class_confirmed == "asleep"):
        return "fire"
    return "hold"


def iso_at(epoch_seconds: float) -> str:
    """A specific instant in the same wire format. Used to back-stamp a debounced sleep edge to
    when it actually happened, so hysteresis costs us notification latency but never log
    accuracy."""
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- debounce / hysteresis (WAVE: "notifications are 2 minutes behind") -----------------
#
# Measured against 14 days of `sensor.ryleighs_sock_sleep_state` (949 samples) on Brandon's
# HA box. The sock's raw signal FLAPS: of 360 "awake" runs, 156 (43%) lasted under 3 minutes
# — Owlet's staging algorithm twitching mid-nap, not Ryleigh actually waking. Real wakes have
# a median run of 4 min and a p75 of 8 min, so a 3-minute confirmation window separates them
# cleanly.
#
# Acting on the RAW signal is what produced all three symptoms Taylor felt:
#   * 70 "She's waking up" + 70 "She's fallen asleep" pushes in 24h, the wake ones
#     time-sensitive so they pierce Sleep Focus,
#   * every one of them contradicted ~60s later, so the banner in her hand describes a state
#     that already ended — the "2 minutes behind" complaint (delivery itself is 1-15s;
#     measured against the relay's own delivery log, it was never the problem),
#   * one overnight stretch logged as ELEVEN separate sleeps instead of one night.
#
# So: hold a reading until it has persisted, and only then treat it as real. This trades a
# little time-to-notify for a signal that is actually true when it arrives.

# Tuned by replaying 10.2 days of the real signal (59,568 simulated 15s polls) through the
# filter and sweeping the knobs — not picked by feel. Per DAY, alerts land at:
#
#              raw (1.3.0)   hold=180   hold=300   hold=420   hold=600
#   wake/asleep      66.7        23.7       18.3       14.1        10.4
#
# 300s is the knee. Below it the sock's twitching leaks through; above it we start delaying
# real wakes for a shrinking return. At 300s a "she's awake" alert means she has been awake
# for five continuous minutes — which is what a parent means by awake.
WAKE_HOLD_SECONDS = 300.0     # awake <-> asleep must persist 5 min to be believed
WAKE_ALERT_MIN_GAP = 900.0    # >=15 min between wake alerts
ASLEEP_ALERT_MIN_GAP = 2700.0 # >=45 min between "she's down" notes (the least actionable one)

# Stage notes are DEEP-SLEEP ONLY. Announcing light sleep too costs ~16 extra pushes a day and
# tells you nothing — light sleep is simply where a newborn spends most of the night. Deep
# sleep is the one worth knowing ("you have a real window"), and at ~2.7/day it stays a signal.
STAGE_HOLD_SECONDS = 120.0    # a stage must persist 2 min (deep runs have a ~2 min median)
STAGE_ALERT_MIN_GAP = 2700.0  # >=45 min between VISIBLE stage notes, per stage
ALERTING_STAGES = ("deep_sleep",)


@dataclass
class Debounced:
    """Hysteresis state for one signal. `confirmed` is what we've told the parents; `candidate`
    is a different reading we're currently timing. Serialized into the relay's config table so
    it survives restarts (and reseeds silently on a cold start — no alert storm on deploy)."""
    confirmed: Optional[str] = None
    candidate: Optional[str] = None
    since: float = 0.0

    def to_json(self) -> str:
        return json.dumps({"confirmed": self.confirmed, "candidate": self.candidate,
                           "since": self.since})

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "Debounced":
        """Tolerant of missing/corrupt/legacy values — a bad row must never wedge the poller.
        A plain (non-JSON) string is read as a legacy bare `confirmed` value, so upgrading from
        1.3.0's `owlet_stage`/`owlet_sleep_cls` keys doesn't re-announce the current state."""
        if not raw:
            return cls()
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return cls(confirmed=d.get("confirmed"), candidate=d.get("candidate"),
                           since=float(d.get("since") or 0.0))
            raise ValueError
        except Exception:
            return cls(confirmed=raw if isinstance(raw, str) else None)


def debounce(state: Debounced, reading: Optional[str], now: float,
             hold: float) -> tuple[Debounced, Optional[str]]:
    """Advance a hysteresis filter one tick.

    Returns `(new_state, newly_confirmed)`. `newly_confirmed` is non-None ONLY on the tick where
    a different reading has held continuously for `hold` seconds — that's the edge worth
    notifying on. Everything else returns None, so callers can fire unconditionally.

    The very first reading seeds `confirmed` SILENTLY (no notification): on a fresh deploy we
    adopt whatever is true right now rather than announcing it.
    """
    if reading is None:
        return state, None                       # nothing to say; hold the candidate timer
    if state.confirmed is None:
        return Debounced(confirmed=reading), None            # seed silently
    if reading == state.confirmed:
        return Debounced(confirmed=reading), None            # back to the known state; reset
    if reading != state.candidate:
        return Debounced(state.confirmed, reading, now), None  # a new candidate starts the clock
    if now - state.since >= hold:
        return Debounced(confirmed=reading), reading         # held long enough — believe it
    return state, None                                       # still counting


def stage_alert_due(last_alert_ts: dict, stage: str, now: float,
                    min_gap: float = STAGE_ALERT_MIN_GAP) -> bool:
    """Should a VISIBLE stage note fire for `stage`? Rate-limited PER STAGE, not from one shared
    budget — 1.3.0 used a single 10-minute gate across every stage, so a light-sleep ping ate the
    budget and the deep-sleep ping that followed was DROPPED (not deferred). That is why only 12
    stage notes went out in 48 hours, and why the one that did arrive described a later
    transition than the one Taylor had noticed."""
    previous = last_alert_ts.get(stage)
    if previous is None:
        return True                      # never announced this stage — always due
    return now - float(previous) >= min_gap
