#!/usr/bin/env python3
"""ApexSight Push Bridge.

Subscribes to Frigate's MQTT review stream and forwards each new alert to your
ApexSight push relay. Holds NO Apple secrets — just your relay URL + a pairing
code. The relay signs and delivers the actual APNs push.

All configuration comes from environment variables (set by run.sh from the HA
addon options + MQTT service):

  RELAY_URL          e.g. https://relay.plexserver525.com
  PAIRING_CODE       e.g. APEX-7F3K-2Q9P  (shown in the app)
  FRIGATE_BASE_URL   externally-reachable Frigate URL for snapshots/GIFs
  TOPIC              MQTT topic (default frigate/reviews)
  ALERTS_ONLY        "true" → only severity=alert; else also detections
  MQTT_HOST/PORT/USER/PASSWORD
"""
import json
import os
import sqlite3
import sys
import time

import paho.mqtt.client as mqtt
import requests

RELAY_URL = os.environ.get("RELAY_URL", "").rstrip("/")
PAIRING_CODE = os.environ.get("PAIRING_CODE", "").upper().strip()
FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "").rstrip("/")
TOPIC = os.environ.get("TOPIC", "frigate/reviews")
ALERTS_ONLY = os.environ.get("ALERTS_ONLY", "true").lower() in ("true", "1", "yes")

# Frigate's per-object events topic (same prefix as the reviews topic) — accumulated
# into the shared relay DB so the relay can build the daily recap without querying
# Frigate over HTTP. The relay reads from the same SQLite file.
EVENTS_TOPIC = (TOPIC[: -len("/reviews")] + "/events") if TOPIC.endswith("/reviews") else "frigate/events"
# House mode (home/night/away) — an HA automation publishes it here on Alarmo state change; we
# forward it to the relay's /v1/mode so app-closed pushes follow the mode. Retained, so a bridge
# restart re-reads the current mode immediately.
MODE_TOPIC = os.environ.get("MODE_TOPIC", "apexsight/mode")
DB_PATH = os.path.join(os.environ.get("APEX_DATA_DIR", "/data"), "relay.db")

MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883") or "1883")
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

# Per-review notification stages already sent: "alert" (the instant push) and
# "final" (the follow-up push carrying the complete GIF). Keyed by review id so a
# review that emits new→update→end fires at most once per stage. TTL-bounded so
# it can't grow forever.
_notified: dict[str, dict[str, float]] = {}
_NOTIFIED_TTL = 3600

# HomeKit-style AI-description follow-up: Frigate writes a GenAI description a few seconds AFTER the
# event (published on `frigate/tracked_object_update`). Send the instant alert first, then a
# follow-up that REPLACES it in place (shared collapse id) once the description lands. The relay
# enforces the household's per-camera opt-out (synced from the app).
DESC_TOPIC = os.environ.get("DESC_TOPIC", "frigate/tracked_object_update")
AI_DESCRIPTIONS = os.environ.get("AI_DESCRIPTIONS", "true").lower() in ("true", "1", "yes")

# tracked-object (detection) id -> {review_id, title, camera, apex_url, media, _t} from the alert,
# so a later description update can be matched back to the same notification.
_pending: dict[str, dict] = {}
_described: dict[str, float] = {}
_PENDING_TTL = 900


def _concise(desc: str, limit: int = 180) -> str:
    """Trim a verbose GenAI paragraph to a notification-friendly first sentence."""
    desc = " ".join((desc or "").split())
    if not desc:
        return ""
    for sep in (". ", "! ", "? "):
        i = desc.find(sep)
        if 0 < i <= limit:
            return desc[: i + 1]
    return desc if len(desc) <= limit else desc[: limit - 1].rstrip() + "\u2026"


def _prune_pending() -> None:
    now = time.time()
    for k in [k for k, v in list(_pending.items()) if now - v.get("_t", 0) > _PENDING_TTL]:
        _pending.pop(k, None)
    for k in [k for k, t in list(_described.items()) if now - t > _PENDING_TTL]:
        _described.pop(k, None)


def log(*a):
    print("[bridge]", *a, file=sys.stdout, flush=True)


def _ensure_recap_table() -> None:
    """The relay's db.init() also creates this — but the bridge may write first."""
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS recap_events ("
                " pairing_code TEXT NOT NULL, event_id TEXT NOT NULL, camera TEXT,"
                " label TEXT, sub_label TEXT, ts REAL NOT NULL,"
                " PRIMARY KEY (pairing_code, event_id))"
            )
            conn.commit()
    except Exception as exc:
        log("recap table init failed:", exc)


def _record_event(after: dict) -> None:
    """Upsert a Frigate tracked-object event into the shared DB for the daily recap.
    Upserting by id keeps the latest sub-label (e.g. a person resolving to a face)."""
    event_id = after.get("id")
    if not event_id:
        return
    sub = after.get("sub_label")
    if isinstance(sub, list):
        sub = sub[0] if sub else None
    ts = after.get("start_time") or time.time()
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute(
                "INSERT INTO recap_events(pairing_code, event_id, camera, label, sub_label, ts) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(pairing_code, event_id) DO UPDATE SET "
                "  camera=excluded.camera, label=excluded.label, "
                "  sub_label=excluded.sub_label, ts=excluded.ts",
                (PAIRING_CODE, event_id, after.get("camera"), after.get("label"), sub, float(ts)),
            )
            conn.commit()
    except Exception as exc:
        log("recap event write failed:", exc)


LABEL_EMOJI = {
    "person": "\U0001f9cd", "car": "\U0001f697", "truck": "\U0001f69a",
    "dog": "\U0001f415", "cat": "\U0001f408", "package": "\U0001f4e6",
    "bicycle": "\U0001f6b2", "motorcycle": "\U0001f3cd", "bird": "\U0001f426",
}


def _titleize(s: str) -> str:
    return s.replace("_", " ").title() if s else s


def _epoch(eid: str) -> float:
    """Start epoch baked into a Frigate event id (`1783198550.714144-xxxx`)."""
    try:
        return float(eid.split("-")[0])
    except (ValueError, IndexError):
        return float("inf")


def _primary_detection(data: dict) -> str | None:
    """The detection whose moment matches the review's canonical thumbnail (`thumb_time`).
    Frigate re-links long-lived parked tracks into fresh reviews, so `detections[0]` (raw MQTT
    order) is frequently the WRONG moment — the "wrong snapshot on the notification" bug. Mirror
    of the iOS app's FrigateClient.primaryDetectionID: the latest detection starting at/just
    before thumb_time; nearest, then earliest, as fallbacks."""
    dets = data.get("detections", []) or []
    if not dets:
        return None
    tt = data.get("thumb_time")
    if tt is not None:
        at_or_before = [d for d in dets if _epoch(d) <= tt + 1]
        if at_or_before:
            return max(at_or_before, key=_epoch)
        return min(dets, key=lambda d: abs(_epoch(d) - tt))
    return min(dets, key=_epoch)


def _build_alert(after: dict, final: bool = False) -> dict | None:
    review_id = after.get("id")
    camera = after.get("camera", "")
    severity = after.get("severity", "")
    if not review_id or not camera:
        return None
    if ALERTS_ONLY and severity != "alert":
        return None

    data = after.get("data", {}) or {}
    objects = data.get("objects", []) or []
    sublabels = [s for s in (data.get("sub_labels", []) or []) if s]
    zones = data.get("zones", []) or []
    detections = data.get("detections", []) or []
    det = _primary_detection(data)   # the detection at the review's thumbnail moment

    obj = objects[0] if objects else "motion"
    emoji = LABEL_EMOJI.get(obj, "\U0001f4f9")
    title = f"{emoji} " + (_titleize(sublabels[0]) if sublabels else ", ".join(_titleize(o) for o in objects) or "Camera activity")

    body_parts = [_titleize(camera)]
    if sublabels:
        body_parts.append(", ".join(_titleize(s) for s in sublabels))
    if zones:
        body_parts.append("Zone: " + ", ".join(_titleize(z) for z in zones))
    body = " • ".join(body_parts)

    payload = {
        "pairing_code": PAIRING_CODE,
        # title/body are a fallback — the relay re-renders from the raw fields below
        # using the household's saved style, so the in-app GUI controls the content.
        "title": title,
        "body": body,
        "camera": camera,
        "review_id": review_id,
        "apex_url": f"apex://review?id={review_id}",
        # Reuse the review id as the APNs collapse id so the follow-up full-GIF
        # push replaces the instant alert in place rather than stacking a dup.
        "collapse_id": review_id,
        # The final update swaps in the complete GIF silently (no second buzz).
        "silent": final,
        # Raw event fields for relay-side, style-driven rendering.
        "camera_name": _titleize(camera),
        "labels": objects,
        "sub_labels": sublabels,
        "zones": zones,
        "severity": severity,
        "stage": "final" if final else "alert",
        "frigate_base_url": FRIGATE_BASE_URL,
    }
    plate = data.get("recognized_license_plate") or ""
    if plate:
        payload["recognized_license_plate"] = plate
    if det:
        payload["detection_id"] = det
    # Rich media, two-stage (matches the SgtBatten blueprint feel):
    #   • instant alert  → a tight CROPPED snapshot (bbox) that reads great on the
    #     lock screen the moment the event starts;
    #   • final update   → the now-complete animated GIF, swapped in place via the
    #     shared collapse id (no duplicate notification).
    if FRIGATE_BASE_URL and det:
        cropped = f"{FRIGATE_BASE_URL}/api/events/{det}/snapshot.jpg?bbox=1&crop=1"
        gif = f"{FRIGATE_BASE_URL}/api/events/{det}/preview.gif"
        full_snapshot = f"{FRIGATE_BASE_URL}/api/events/{det}/snapshot.jpg"
        if final:
            payload["snapshot_url"] = gif
            payload["thumbnail_url"] = cropped
        else:
            payload["snapshot_url"] = cropped
            payload["thumbnail_url"] = full_snapshot
        if AI_DESCRIPTIONS and not final:
            _prune_pending()
            record = {
                "review_id": review_id,
                "title": title,
                "camera": camera,
                "apex_url": payload["apex_url"],
                "snapshot_url": gif,
                "thumbnail_url": cropped,
                "_t": time.time(),
            }
            for _d in detections:
                _pending[_d] = record
    return payload


def _stages_to_send(after: dict, msg_type: str) -> list[str]:
    """Decide which notification stages to send for this MQTT update.

    Returns any of "alert" (the instant push) and "final" (the follow-up push
    carrying the complete GIF, sent once the review has ended).
    """
    review_id = after.get("id")
    if not review_id:
        return []
    now = time.time()
    # prune anything past the TTL
    for k in [k for k, v in list(_notified.items()) if now - max(v.values(), default=0) > _NOTIFIED_TTL]:
        _notified.pop(k, None)
    sent = _notified.get(review_id, {})
    detections = (after.get("data", {}) or {}).get("detections", []) or []
    severity = after.get("severity", "")

    stages: list[str] = []
    # Instant alert as soon as there's a detection (so we have a GIF), or at the
    # very latest when the review ends. With ALERTS_ONLY, gate on "alert" severity to
    # match _build_alert's own filter: otherwise a review first seen as a plain
    # "detection" would get its "alert" stage stamped as sent below while _build_alert
    # posts nothing — and when it LATER escalates to "alert", it'd be deduped and the
    # real alert silently dropped (a missed alert).
    alert_allowed = (not ALERTS_ONLY) or severity == "alert"
    if "alert" not in sent and alert_allowed and (detections or msg_type == "end"):
        stages.append("alert")
    # A separate "final" update only makes sense if the instant alert already went
    # out *earlier*, while the event was still in progress — only then is its GIF
    # partial. If the review ends in the same update that first fires the alert,
    # that GIF is already complete, so no follow-up is needed.
    if msg_type == "end" and detections and "alert" in sent and "final" not in sent:
        stages.append("final")

    if stages:
        record = _notified.setdefault(review_id, {})
        for s in stages:
            record[s] = now
    return stages


def _post_to_relay(payload: dict, stage: str, attempts: int = 3) -> None:
    """POST one alert to the relay, retrying transient failures with backoff.

    A missed alert is the worst failure mode for a security app, so retry a few
    times before giving up rather than dropping the event on the first blip.
    Relay 2xx/4xx are final answers (don't hammer); only 5xx and network errors
    are retried.
    """
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(f"{RELAY_URL}/v1/notify", json=payload, timeout=10)
            log(f"forwarded review {payload['review_id']} [{stage}] → {r.status_code} {r.text[:120]}")
            if r.status_code < 500:
                return
            log(f"relay {r.status_code}, retrying ({attempt}/{attempts})")
        except Exception as exc:
            log(f"relay POST failed ({attempt}/{attempts}):", exc)
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2


def _post_mode(mode: str) -> None:
    """Forward the house mode to the relay's /v1/mode. Best-effort with a couple of retries — a
    missed mode update just leaves the previous mode in place (fail-open on the relay side)."""
    mode = (mode or "").strip().lower()
    if not mode:
        return
    for attempt in range(1, 3):
        try:
            r = requests.post(f"{RELAY_URL}/v1/mode", json={"mode": mode, "pairing_code": PAIRING_CODE}, timeout=10)
            log(f"house mode → {mode}: relay {r.status_code} {r.text[:80]}")
            if r.status_code < 500:
                return
        except Exception as exc:
            log(f"mode POST failed ({attempt}/2):", exc)
        time.sleep(1.0)


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log(f"connected to MQTT {MQTT_HOST}:{MQTT_PORT}, subscribing {TOPIC} + {EVENTS_TOPIC}")
        client.subscribe(TOPIC)
        client.subscribe(EVENTS_TOPIC)
        client.subscribe(MODE_TOPIC)
        log(f"subscribing {MODE_TOPIC} for house mode")
        if AI_DESCRIPTIONS:
            client.subscribe(DESC_TOPIC)
            log(f"subscribing {DESC_TOPIC} for AI descriptions")
    else:
        log(f"MQTT connect failed rc={rc}")


def _handle_description(event: dict) -> None:
    """A GenAI description landed for a tracked object — send a follow-up that replaces the
    original alert with the description. The relay applies the household's per-camera choice."""
    if event.get("type") != "description":
        return
    eid = event.get("id")
    desc = _concise(event.get("description", ""))
    if not eid or not desc:
        return
    rec = _pending.get(eid)
    if not rec:
        return  # no recent alert for this object
    now = time.time()
    if eid in _described and now - _described[eid] < _PENDING_TTL:
        return
    _described[eid] = now
    payload = {
        "pairing_code": PAIRING_CODE,
        "title": rec["title"],
        "body": desc,
        "camera": rec["camera"],
        "review_id": rec["review_id"],
        "apex_url": rec.get("apex_url", ""),
        "collapse_id": rec["review_id"],   # replace the original alert in place
        "is_description": True,            # relay honors the per-camera opt-out
        "announce": True,                  # read aloud in CarPlay (no second buzz)
    }
    if rec.get("snapshot_url"):
        payload["snapshot_url"] = rec["snapshot_url"]
    if rec.get("thumbnail_url"):
        payload["thumbnail_url"] = rec["thumbnail_url"]
    _post_to_relay(payload, "description")


def on_message(client, userdata, msg):
    # House mode arrives as a plain string ("home"/"night"/"away"), not JSON — handle it before the
    # JSON decode below (which would otherwise reject it).
    if msg.topic == MODE_TOPIC:
        _post_mode(msg.payload.decode("utf-8", "ignore"))
        return
    try:
        event = json.loads(msg.payload.decode("utf-8"))
    except Exception as exc:
        log("bad payload:", exc)
        return
    if AI_DESCRIPTIONS and msg.topic == DESC_TOPIC:
        _handle_description(event)
        return
    after = event.get("after") or event.get("before") or {}
    # The events topic only feeds the daily-recap tally, not notifications.
    if msg.topic == EVENTS_TOPIC:
        _record_event(after)
        return
    msg_type = event.get("type", "")
    stages = _stages_to_send(after, msg_type)
    if not stages:
        return
    for stage in stages:
        payload = _build_alert(after, final=(stage == "final"))
        if payload:
            _post_to_relay(payload, stage)


def main():
    if not RELAY_URL or not PAIRING_CODE:
        log("FATAL: relay_url and pairing_code are required in the addon options.")
        sys.exit(1)
    if not FRIGATE_BASE_URL:
        log("WARNING: frigate_base_url is empty — notifications will have no image.")

    _ensure_recap_table()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as exc:
            log("MQTT loop error, retrying in 10s:", exc)
            time.sleep(10)


if __name__ == "__main__":
    main()
