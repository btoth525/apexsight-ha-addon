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


def _owlet_role(entity_id: str) -> Optional[str]:
    l = entity_id.lower()
    if not _is_owlet(l):
        return None
    if "heart" in l:
        return "hr"
    if "oxygen" in l or "spo2" in l:
        return "o2"
    if "skin" in l or "temp" in l:
        return "temp"
    if "battery" in l:
        return "battery"
    if "charg" in l:
        return "charging"
    return "sock_on"   # any remaining owlet-ish binary_sensor: treat as the connected flag


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
    except (ValueError, TypeError):
        pass   # unavailable/unknown states — leave the field unset rather than crash


def classify(states: list[dict]) -> dict:
    """Pure function: HA `GET /states` response → {vitals, nursery}. No network, fully
    unit-testable. `connected` is added by the async wrapper (it reflects the API call,
    not the classification)."""
    vitals = {"bpm": None, "spo2": None, "skin_temp_f": None, "battery_pct": None,
              "sock_on": False, "charging": False}
    saw_owlet = False
    nursery: list[dict] = []

    for s in states:
        entity_id = s.get("entity_id", "")
        state = s.get("state", "")
        attrs = s.get("attributes") or {}
        if state in ("unavailable", "unknown"):
            continue

        role = _owlet_role(entity_id)
        if role:
            saw_owlet = True
            _apply_owlet(vitals, role, state)
            continue

        name = (attrs.get("friendly_name") or entity_id)
        if "nursery" not in name.lower() and "nursery" not in entity_id.lower():
            continue
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        is_toggle = domain in ("switch", "light", "input_boolean", "fan")
        unit = attrs.get("unit_of_measurement") or ""
        nursery.append({
            "id": entity_id,
            "label": name,
            "value": ("On" if state == "on" else "Off") if is_toggle else f"{state}{unit}",
            "is_toggle": is_toggle,
            "is_on": state == "on",
        })

    return {"vitals": vitals if saw_owlet else None, "nursery": nursery}


# ---- async wrappers (network) -----------------------------------------------

async def state() -> dict:
    states = await _get("/states")
    if states is None:
        return {"connected": False, "vitals": None, "nursery": []}
    result = classify(states)
    result["connected"] = True
    return result


async def toggle(entity_id: str) -> bool:
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "homeassistant"
    return await _post(f"/services/{domain}/toggle", {"entity_id": entity_id})
