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
