"""The house, read server-side (plan §6.2/§6.3) — so the app needs ZERO Home Assistant
setup. This add-on already runs inside HA, so with `homeassistant_api: true` in
config.yaml, Supervisor injects `SUPERVISOR_TOKEN` and proxies Core API calls for us —
no long-lived access token to create or paste anywhere.

Entities are auto-discovered by name (no entity picker, on either side):
  - Owlet Dream Sock vitals: any entity whose id/name mentions owlet/dream_sock/sock.
  - Nursery strip: any entity whose id/name mentions "nursery".
As soon as those exist in HA (Owlet signed in, entities named/aliased), they appear.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
CORE_API = "http://supervisor/core/api"


async def _get(path: str) -> Optional[Any]:
    if not SUPERVISOR_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{CORE_API}{path}", headers=headers)
            if r.status_code != 200:
                return None
            return r.json()
    except httpx.HTTPError:
        return None


async def _post(path: str, body: dict) -> bool:
    if not SUPERVISOR_TOKEN:
        return False
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{CORE_API}{path}", headers=headers, json=body)
            return r.status_code in (200, 201)
    except httpx.HTTPError:
        return False


# ---- pure classification (tested without any network) -----------------------

def _is_owlet(entity_id: str) -> bool:
    l = entity_id.lower()
    return "owlet" in l or "dream_sock" in l or "sock" in l


# Owlet's OWN alert flags (binary_sensors). We RELAY these — Lulla never invents medical
# thresholds; the sock/base station decides, we just forward. `awake` is Owlet's clean sleep
# signal (better than parsing the sleep_state string).
_ALERT_NEEDLES = {
    "high_heart_rate": "high_hr", "low_heart_rate": "low_hr",
    "high_oxygen": "high_o2", "low_oxygen": "low_o2",
    "low_battery": "low_battery", "lost_power": "lost_power",
    "sock_disconnected": "sock_disconnected", "sock_off": "sock_off",
}


def _owlet_alert(entity_id: str) -> Optional[str]:
    """Alert-flag key for an Owlet binary_sensor, or 'awake', or None. Checked BEFORE _owlet_role
    so e.g. 'high_heart_rate_alert' is an alert, not misread as the heart-rate vital."""
    l = entity_id.lower()
    if not _is_owlet(l):
        return None
    for needle, key in _ALERT_NEEDLES.items():
        if needle in l:
            return key
    if "awake" in l:
        return "awake"
    return None


def _owlet_role(entity_id: str) -> Optional[str]:
    l = entity_id.lower()
    if not _is_owlet(l):
        return None
    if "heart" in l:
        return "hr"
    if "sleep" in l:            # sensor.<baby>_sock_sleep_state — drives auto sleep logging
        return "sleep_state"
    if "signal" in l:          # Wi-Fi signal strength — a monitor-health signal, not a vital
        return "signal"
    # O2 saturation. The HA entity is "..._o2_saturation" (NOT "oxygen"/"spo2"), which the old
    # check missed entirely — so O2 fell through to the sock_on fallback and was never captured.
    if "o2" in l or "oxygen" in l or "spo2" in l:
        if "average" in l:     # ignore the 10-minute-average entity; the live reading wins
            return None
        return "o2"
    if "skin" in l or "temp" in l:
        return "temp"
    if "battery" in l:
        if "remaining" in l:   # a duration estimate, not the percentage — ignore
            return None
        return "battery"
    if "charg" in l:
        return "charging"
    return "sock_on"   # any remaining owlet-ish binary_sensor: treat as the connected flag


def _guess_baby_name(entity_id: str, friendly_name: Optional[str]) -> Optional[str]:
    """HA's Owlet integration names the device "<Kid's Name> Sock" (a possessive like
    "Ryleigh's" has its apostrophe sanitized away, leaving "Ryleighs"), then suffixes each
    entity with its sensor type, e.g. "Ryleighs Sock Heart Rate". Recover the kid's name as
    a best-guess suggestion — the app always shows it as an editable prefill during
    onboarding, never a silent override of anything the user typed."""
    raw = friendly_name or entity_id.split(".", 1)[-1].replace("_", " ")
    lower = raw.lower()
    idx = lower.find("sock")
    if idx <= 0:
        return None
    prefix = raw[:idx].strip()
    if not prefix:
        return None
    if prefix.lower().endswith("s") and len(prefix) > 1:
        prefix = prefix[:-1]
    prefix = prefix.strip()
    return prefix.title() if prefix else None


def _apply_owlet(vitals: dict, role: str, state: str) -> None:
    try:
        if role == "hr":
            vitals["bpm"] = int(float(state))
        elif role == "o2":
            vitals["spo2"] = int(float(state))
        elif role == "temp":
            vitals["skin_temp_f"] = float(state)
        elif role == "battery":
            vitals["battery_pct"] = int(float(state))
        elif role == "charging":
            vitals["charging"] = state == "on"
        elif role == "sock_on":
            vitals["sock_on"] = state == "on"
        elif role == "sleep_state":
            vitals["sleep_state"] = state          # raw HA value, e.g. "awake"/"light"/"deep"
        elif role == "signal":
            vitals["signal"] = int(float(state))
    except (ValueError, TypeError):
        pass   # unavailable/unknown states — leave the field unset rather than crash


def classify(states: list[dict]) -> dict:
    """Pure function: HA `GET /states` response → {vitals, nursery, baby_name}. No network,
    fully unit-testable. `connected` is added by the async wrapper (it reflects the API
    call, not the classification)."""
    vitals = {"bpm": None, "spo2": None, "skin_temp_f": None, "battery_pct": None,
              "sock_on": False, "charging": False, "sleep_state": None, "signal": None}
    alerts: dict = {}                      # Owlet's own flags: {key: bool} (only when available)
    saw_owlet_data = False   # at least one owlet reading is actually available
    baby_name: Optional[str] = None
    nursery: list[dict] = []

    for s in states:
        entity_id = s.get("entity_id", "")
        state = s.get("state", "")
        attrs = s.get("attributes") or {}
        friendly_name = attrs.get("friendly_name") or entity_id

        alert_key = _owlet_alert(entity_id)
        if alert_key:
            if baby_name is None:
                baby_name = _guess_baby_name(entity_id, friendly_name)
            if state in ("on", "off"):     # only when the flag is actually reporting
                alerts[alert_key] = (state == "on")
            continue

        role = _owlet_role(entity_id)
        if role:
            # The device's NAME is static — worth reading even while the sock itself is
            # offline/unavailable, unlike the readings below which need a live value.
            if baby_name is None:
                baby_name = _guess_baby_name(entity_id, friendly_name)
            if state not in ("unavailable", "unknown"):
                saw_owlet_data = True
                _apply_owlet(vitals, role, state)
            continue

        if state in ("unavailable", "unknown"):
            continue
        if "nursery" not in friendly_name.lower() and "nursery" not in entity_id.lower():
            continue
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        is_toggle = domain in ("switch", "light", "input_boolean", "fan")
        unit = attrs.get("unit_of_measurement") or ""
        nursery.append({
            "id": entity_id,
            "label": friendly_name,
            "value": ("On" if state == "on" else "Off") if is_toggle else f"{state}{unit}",
            "is_toggle": is_toggle,
            "is_on": state == "on",
        })

    # `sock_on` = is she actually wearing it. There is no single HA entity for this (the sock's
    # binary_sensors are all alert flags), and the old fallback silently left it False forever —
    # which made the app show "Awake · Off" while live vitals proved otherwise. Derive it from
    # EVIDENCE instead: a live heart-rate/O2 reading means it's on her, unless Owlet's own
    # `sock_off` flag says it came off.
    has_reading = vitals["bpm"] is not None or vitals["spo2"] is not None
    vitals["sock_on"] = bool(has_reading) and alerts.get("sock_off") is not True

    return {"vitals": vitals if saw_owlet_data else None, "alerts": alerts,
            "nursery": nursery, "baby_name": baby_name}


# ---- async wrappers (network) -----------------------------------------------

async def state() -> dict:
    states = await _get("/states")
    if states is None:
        return {"connected": False, "vitals": None, "alerts": {}, "nursery": [], "baby_name": None}
    result = classify(states)
    result["connected"] = True
    return result


def _downsample(points: list[dict], cap: int = 150) -> list[dict]:
    """Keep the payload small for the phone: evenly thin a series to at most `cap` points.
    Always keeps the newest sample so the chart's right edge is current."""
    if len(points) <= cap:
        return points
    step = len(points) / cap
    thinned = [points[int(i * step)] for i in range(cap)]
    if thinned[-1] is not points[-1]:
        thinned[-1] = points[-1]
    return thinned


async def vitals_history(hours: int = 12) -> dict:
    """Heart-rate / O2 / skin-temp series from HA's recorder, for the app's trend charts.
    Returns {"hr": [{t, v}], "spo2": [...], "temp": [...]}, oldest→newest, thinned for transport.
    Empty lists (not an error) when the sock hasn't been worn in the window."""
    states = await _get("/states")
    if states is None:
        return {"connected": False, "hours": hours, "hr": [], "spo2": [], "temp": []}

    wanted: dict[str, str] = {}          # entity_id -> series key
    for s in states:
        eid = s.get("entity_id", "")
        if _owlet_alert(eid):            # alert flags are not vitals
            continue
        role = _owlet_role(eid)
        if role in ("hr", "o2", "temp") and role not in wanted.values():
            wanted[eid] = {"hr": "hr", "o2": "spo2", "temp": "temp"}[role]
    if not wanted:
        return {"connected": True, "hours": hours, "hr": [], "spo2": [], "temp": []}

    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    raw = await _get(f"/history/period/{start}"
                     f"?filter_entity_id={','.join(wanted)}&minimal_response&no_attributes")
    out: dict[str, list] = {"hr": [], "spo2": [], "temp": []}
    for series in (raw or []):
        if not series:
            continue
        key = wanted.get(series[0].get("entity_id", ""))
        if not key:
            continue
        pts = []
        for row in series:
            state = row.get("state")
            when = row.get("last_changed") or row.get("last_updated")
            if not state or not when or state in ("unavailable", "unknown"):
                continue          # sock off the foot — a gap, not a zero
            try:
                pts.append({"t": when, "v": float(state)})
            except (TypeError, ValueError):
                continue
        out[key] = _downsample(pts)
    return {"connected": True, "hours": hours, **out}


async def toggle(entity_id: str) -> bool:
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "homeassistant"
    return await _post(f"/services/{domain}/toggle", {"entity_id": entity_id})
