"""Per-device notification delivery gate — the app-closed mirror of the iOS app's
`NotificationPreferencesStore.wouldDeliver`.

The app syncs each device's notification preferences (keyed by device token) to the relay via
`POST /v1/device-prefs`; `would_deliver` re-evaluates the SAME decision here so an app-closed push
is suppressed/allowed exactly as the foreground app would.

FAIL-OPEN IS LAW. This gates a security + baby-monitor system: a missed real alert is categorically
worse than a stray buzz. Every uncertainty — no prefs synced yet, malformed JSON, a key missing, a
timezone in doubt, an unexpected exception — MUST default to DELIVER. The only way to suppress is an
affirmative, parsed, confident "this is muted."

SOFT-ONLY INVARIANT — do NOT put `disarmed` or global `snoozed_until` in the per-device `prefs`
blob. Those are set in real time from six contexts (app, widget, Siri, watch, CarPlay, Focus) via
the *household* `gate:{code}`, and that early-return stays the source of truth for them. The blob is
synced only on app settings-change/foreground, so a stale `disarmed:true` sitting in it would
suppress a live alert AFTER the user re-armed from a widget — a fail-CLOSED miss, the one thing this
file exists to prevent. Per-camera snooze is safe in the blob precisely because it's an app-only,
self-expiring timestamp: stale → expires → fail-open. Global snooze-all isn't (Siri can set it), so
it stays household. The `disarmed`/`snoozed_until` branches below remain only as inert, tested
defense-in-depth — the per-device path never populates them.
"""
from __future__ import annotations
from typing import Any


def _f(v: Any) -> float:
    """Best-effort float; 0.0 on anything unparseable (so a bad snooze value never suppresses)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _local_minutes(tz_offset: Any, now_epoch: float) -> int | None:
    """Minutes-since-local-midnight for the device, or None if we can't be sure (→ caller
    treats 'not quiet', i.e. fail-open). `tz_offset` is seconds from GMT (app sends it, refreshed
    every foreground so it tracks DST)."""
    try:
        off = float(tz_offset)
    except (TypeError, ValueError):
        return None
    local = now_epoch + off
    return int((local % 86400) // 60)


def _is_quiet_now(quiet: dict, tz_offset: Any, now_epoch: float) -> bool:
    """True only when we can CONFIDENTLY place 'now' inside an enabled quiet-hours window.
    Any doubt → False (deliver)."""
    if not isinstance(quiet, dict) or not quiet.get("enabled"):
        return False
    cur = _local_minutes(tz_offset, now_epoch)
    if cur is None:
        return False
    try:
        start = int(quiet["start"])
        end = int(quiet["end"])
    except (KeyError, TypeError, ValueError):
        return False
    if start == end:
        return False
    if start <= end:                 # same-day window, e.g. 09:00–17:00
        return start <= cur < end
    return cur >= start or cur < end  # overnight window, e.g. 22:00–07:00


def _trigger_matches(t: dict, camera: str, label: str, zones: list[str], score: float) -> bool:
    """Mirror of NotificationTrigger.matches — an empty list means 'any'."""
    if not isinstance(t, dict) or not t.get("enabled", True):
        return False
    cams = t.get("cameras") or []
    if cams and camera not in cams:
        return False
    labels = t.get("labels") or []
    if labels and label not in labels:
        return False
    req = t.get("required_zones") or []
    if req and not all(z in zones for z in req):
        return False
    if score < _f(t.get("min_confidence")):
        return False
    return True


def would_deliver(prefs: Any, camera: str, label: str, zones: list[str],
                  score: float, now_epoch: float) -> tuple[bool, str]:
    """Return (deliver, reason) for ONE device's synced prefs. Mirrors the iOS gate:
    hard mutes (disarm / global snooze / per-camera snooze) always win; otherwise deliver if the
    soft filters (camera & object & zone enabled, not quiet hours) pass OR any enabled trigger
    matches. FAIL-OPEN: returns (True, ...) on any malformed/absent input or unexpected error."""
    try:
        if not isinstance(prefs, dict):
            return True, "fail-open: prefs not an object"

        # --- Hard mutes: only an affirmative, parsed value suppresses ---
        if prefs.get("disarmed") is True:
            return False, "disarmed"
        gsnooze = _f(prefs.get("snoozed_until"))
        if gsnooze and now_epoch < gsnooze:
            return False, "global snooze"
        cam_snoozes = prefs.get("camera_snoozes")
        if isinstance(cam_snoozes, dict):
            cs = _f(cam_snoozes.get(camera))
            if cs and now_epoch < cs:
                return False, f"camera snooze: {camera}"

        # --- Soft filters (a trigger can re-open any of these) ---
        cameras_disabled = prefs.get("cameras_disabled") or []
        objects_disabled = prefs.get("objects_disabled") or []
        zones_disabled = prefs.get("zones_disabled") or []
        quiet_now = _is_quiet_now(prefs.get("quiet_hours") or {}, prefs.get("tz_offset"), now_epoch)

        camera_ok = camera not in cameras_disabled
        object_ok = label not in objects_disabled
        # Zone OK if the event has no zones, or at least one of its zones is still enabled.
        zone_ok = (not zones) or any(z not in zones_disabled for z in zones)

        if camera_ok and object_ok and zone_ok and not quiet_now:
            return True, "soft filters pass"

        # --- Triggers are additive allow-rules ---
        for t in (prefs.get("triggers") or []):
            if _trigger_matches(t, camera, label, zones, score):
                if t.get("respect_quiet_hours", True) and quiet_now:
                    continue
                return True, f"trigger: {t.get('name', '?')}"

        # Nothing re-opened it — name the first thing that muted it, for the logs.
        if not camera_ok:
            return False, f"camera muted: {camera}"
        if not object_ok:
            return False, f"object muted: {label}"
        if not zone_ok:
            return False, "all zones muted"
        if quiet_now:
            return False, "quiet hours"
        return False, "soft filters failed"
    except Exception as exc:  # noqa: BLE001 — fail-open is the whole point
        return True, f"fail-open: {exc!r}"
