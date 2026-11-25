"""Live Activities for person alerts — one per camera per phone (1.30.0).

When a person alert is delivered to a phone that has Live Activities on, the relay starts a Live
Activity for that camera with the phone's push-to-start token, or — if one is already showing for that
camera — updates it with the per-activity token the app reported. Five minutes after the camera's last
person alert the sweeper ends it. Every start/update carries a stale-date, so even if the relay
restarts or never learns the activity's token, the Lock Screen shows it as stale rather than current.

The ActivityKit types live in the app (`ApexAlertAttributes`); the JSON keys below must match its
`ContentState` / attribute property names exactly.
"""
import asyncio
import json
import time
from typing import Optional

from . import apns, db

LINGER = 300                       # seconds an activity stays after the camera's last person alert
SWEEP_EVERY = 30
ATTRIBUTES_TYPE = "ApexAlertAttributes"
_GONE = ("410", "BadDeviceToken", "Unregistered", "BadEnvironmentKeyInToken", "ExpiredToken")


def is_person(labels: Optional[list]) -> bool:
    for label in labels or []:
        name = (label or "").lower()
        if name.endswith("-verified"):
            name = name[: -len("-verified")]
        if name == "person":
            return True
    return False


def content_state(title: str, body: str, review_id: str, now: int) -> dict:
    return {"title": title[:120], "body": body[:200], "reviewID": review_id, "lastSeen": float(now)}


def _gone(detail: str) -> bool:
    return any(k in detail for k in _GONE)


async def on_person_alert(device_tokens: list, camera: str, camera_name: str, title: str, body: str,
                          review_id: str, labels: Optional[list]) -> None:
    """Fire-and-forget from /v1/notify after the alert went out. Never raises."""
    if not camera or not is_person(labels):
        return
    now = int(time.time())
    for device in device_tokens:
        try:
            await _for_device(device, camera, camera_name, title, body, review_id, now)
        except Exception as exc:  # a Live Activity must never take the alert path down
            print(f"[liveactivity] {device[:8]}… {camera}: {exc!r}", flush=True)


async def _for_device(device: str, camera: str, camera_name: str, title: str, body: str,
                      review_id: str, now: int) -> None:
    start = db.la_start_token_for(device)
    if start is None:
        return
    state = content_state(title, body, review_id, now)
    row = db.la_activity_for(device, camera)
    showing = row is not None and now - int(row["last_alert"]) < LINGER

    if showing and row["activity_token"]:
        payload = {"aps": {"timestamp": now, "event": "update", "content-state": state,
                           "stale-date": now + LINGER}}
        ok, detail = await apns.send_liveactivity(row["activity_token"], row["environment"], payload)
        if ok:
            db.touch_la_activity(device, camera, row["environment"], now, json.dumps(state))
            print(f"[liveactivity] {device[:8]}… {camera}: updated", flush=True)
            return
        print(f"[liveactivity] {device[:8]}… {camera}: update failed {detail}", flush=True)
        if not _gone(detail):
            return                      # transient: don't stack a second activity on the first
        db.delete_la_activity(device, camera)
    elif showing:
        # Started seconds ago and the app hasn't reported its update token yet: one is on screen
        # already, so don't start another.
        db.touch_la_activity(device, camera, row["environment"], now, json.dumps(state))
        return
    elif row is not None:
        db.delete_la_activity(device, camera)   # an old one for this camera: its token is dead weight

    payload = {"aps": {
        "timestamp": now,
        "event": "start",
        "content-state": state,
        "attributes-type": ATTRIBUTES_TYPE,
        "attributes": {"camera": camera, "cameraName": camera_name or camera},
        # No sound: the alert notification itself already rang. This only presents the activity.
        "alert": {"title": title[:120], "body": body[:200]},
        "stale-date": now + LINGER,
    }}
    ok, detail = await apns.send_liveactivity(start["start_token"], start["environment"], payload)
    if ok:
        db.touch_la_activity(device, camera, start["environment"], now, json.dumps(state))
        print(f"[liveactivity] {device[:8]}… {camera}: started", flush=True)
    else:
        print(f"[liveactivity] {device[:8]}… {camera}: start failed {detail}", flush=True)
        if _gone(detail):
            db.delete_la_start_token(device)


async def sweep_once(now: Optional[int] = None) -> int:
    """End activities whose camera has been quiet for LINGER. Returns how many rows were closed."""
    now = int(now if now is not None else time.time())
    closed = 0
    for row in db.la_quiet_activities(now - LINGER):
        token = row["activity_token"]
        if token:
            state = json.loads(row["state"]) if row["state"] else content_state("", "", "", int(row["last_alert"]))
            payload = {"aps": {"timestamp": now, "event": "end", "content-state": state,
                               "dismissal-date": now + 60}}
            ok, detail = await apns.send_liveactivity(token, row["environment"], payload, priority=5)
            if not ok and not _gone(detail):
                continue                # transient: try again next sweep
        db.delete_la_activity(row["device_token"], row["camera"])
        closed += 1
    return closed


async def sweeper() -> None:
    while True:
        await asyncio.sleep(SWEEP_EVERY)
        try:
            await sweep_once()
        except Exception as exc:
            print(f"[liveactivity] sweep failed: {exc!r}", flush=True)
