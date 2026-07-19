"""Pure routing + watchdog decision logic (§7.4 routing rules, §7.7 supervised loop).

Everything here is a pure function of its inputs so the non-negotiable rules are unit
testable without a database, a clock, or Apple. `main.py` does the I/O (iterate devices,
send, log) and delegates the DECISIONS to these functions.
"""
from dataclasses import dataclass
from typing import Optional

# Interruption levels that must pierce quiet hours and nap-aware suppression.
_URGENT = ("time-sensitive", "critical")


def is_quiet_hours(now_minutes: int, start_minutes: int, end_minutes: int) -> bool:
    """True if `now` (minutes since local midnight) falls in [start, end).
    Handles windows that wrap past midnight (e.g. 22:00 → 07:00)."""
    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    # Wrapping window: quiet if after start OR before end.
    return now_minutes >= start_minutes or now_minutes < end_minutes


def hhmm_to_minutes(value: str) -> int:
    """'22:00' → 1320. Tolerant of bad input (falls back to 0)."""
    try:
        h, m = value.strip().split(":")
        return (int(h) % 24) * 60 + (int(m) % 60)
    except Exception:
        return 0


@dataclass
class RouteDecision:
    deliver: bool          # send at all?
    silent: bool           # deliver as a silent background push instead of an alert?
    push_type: str         # "alert" | "background"
    reason: str            # why (for the delivery log)


def route_event(
    *,
    interruption_level: Optional[str],
    in_quiet_hours: bool,
    nap_aware: bool,
    child_asleep: bool,
) -> RouteDecision:
    """Apply §7.4 routing (per recipient device). Actor-exclusion + collapse_id are
    handled by the caller (device iteration / header); this decides deliver vs silent.

    Rules:
      - Quiet hours suppress everything EXCEPT time-sensitive/critical → not delivered.
      - If nap_aware and the child is asleep, downgrade NON-urgent events to silent
        (a background push, no banner/sound).
      - Urgent (time-sensitive/critical) always delivers as a normal alert.
    """
    level = interruption_level or "active"
    urgent = level in _URGENT

    if in_quiet_hours and not urgent:
        return RouteDecision(False, False, "alert", "quiet-hours suppressed")

    if nap_aware and child_asleep and not urgent:
        return RouteDecision(True, True, "background", "nap-aware downgrade to silent")

    return RouteDecision(True, False, "alert", "deliver")


# ---- supervised watchdog (§7.7) ---------------------------------------------

@dataclass
class WatchdogDecision:
    fire: bool             # fire monitoring.chain_broken as a critical alert?
    status: str            # "green" | "amber" | "red"
    reason: str            # which link broke (or "healthy")


def evaluate_watchdog(
    *,
    now: float,
    last_heartbeat: Optional[float],
    heartbeat_timeout: float,
    ha_last_seen: Optional[float],
    ha_timeout: float,
    owlet_unavailable: bool,
    undeliverable: bool,
    enabled: bool = True,
    warn_fraction: float = 0.5,
) -> WatchdogDecision:
    """Decide whether the monitoring chain is broken (§7.7). Pure function of state so
    it's unit-testable. Silence must always mean "watching and fine" — so ANY stale link
    fires `monitoring.chain_broken` as a critical alert.

    A link is BROKEN (red, fire) when:
      - the app's heartbeat is older than `heartbeat_timeout` (or never seen),
      - HA hasn't been seen within `ha_timeout` (or never seen),
      - the Owlet integration reports entities unavailable,
      - a push was undeliverable (the alert channel itself is down).

    The chain is DEGRADING (amber, no fire) when nothing is broken yet but the freshest
    heartbeat/HA signal has aged past `warn_fraction` of its timeout — a soft warning for
    the Today-screen pip before it goes red.
    """
    if not enabled:
        return WatchdogDecision(False, "green", "watchdog disabled")

    broken: list[str] = []
    if last_heartbeat is None or (now - last_heartbeat) > heartbeat_timeout:
        broken.append("app heartbeat missed")
    if ha_last_seen is None or (now - ha_last_seen) > ha_timeout:
        broken.append("HA unreachable")
    if owlet_unavailable:
        broken.append("Owlet entities unavailable")
    if undeliverable:
        broken.append("push undeliverable")

    if broken:
        return WatchdogDecision(True, "red", "; ".join(broken))

    # Not broken — but is a link aging toward the threshold? (heartbeat/HA both present here.)
    hb_ratio = (now - last_heartbeat) / heartbeat_timeout if heartbeat_timeout else 0.0
    ha_ratio = (now - ha_last_seen) / ha_timeout if ha_timeout else 0.0
    if max(hb_ratio, ha_ratio) >= warn_fraction:
        return WatchdogDecision(False, "amber", "degrading")

    return WatchdogDecision(False, "green", "healthy")
