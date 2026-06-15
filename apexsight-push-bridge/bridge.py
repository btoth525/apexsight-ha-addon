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
import sys
import time

import paho.mqtt.client as mqtt
import requests

RELAY_URL = os.environ.get("RELAY_URL", "").rstrip("/")
PAIRING_CODE = os.environ.get("PAIRING_CODE", "").upper().strip()
FRIGATE_BASE_URL = os.environ.get("FRIGATE_BASE_URL", "").rstrip("/")
TOPIC = os.environ.get("TOPIC", "frigate/reviews")
ALERTS_ONLY = os.environ.get("ALERTS_ONLY", "true").lower() in ("true", "1", "yes")

MQTT_HOST = os.environ.get("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883") or "1883")
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

# Review ids we've already pushed, so a review that emits new→update→end only
# notifies once. Bounded so it can't grow forever.
_notified: dict[str, float] = {}
_NOTIFIED_TTL = 3600


def log(*a):
    print("[bridge]", *a, file=sys.stdout, flush=True)


LABEL_EMOJI = {
    "person": "\U0001f9cd", "car": "\U0001f697", "truck": "\U0001f69a",
    "dog": "\U0001f415", "cat": "\U0001f408", "package": "\U0001f4e6",
    "bicycle": "\U0001f6b2", "motorcycle": "\U0001f3cd", "bird": "\U0001f426",
}


def _titleize(s: str) -> str:
    return s.replace("_", " ").title() if s else s


def _build_alert(after: dict) -> dict | None:
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
        "title": title,
        "body": body,
        "camera": camera,
        "review_id": review_id,
        "apex_url": f"apex://review?id={review_id}",
    }
    # Rich media: the first detection's animated GIF (+ static thumbnail fallback).
    if FRIGATE_BASE_URL and detections:
        det = detections[0]
        payload["snapshot_url"] = f"{FRIGATE_BASE_URL}/api/events/{det}/preview.gif"
        payload["thumbnail_url"] = f"{FRIGATE_BASE_URL}/api/events/{det}/snapshot.jpg"
    return payload


def _should_notify(after: dict, msg_type: str) -> bool:
    review_id = after.get("id")
    if not review_id:
        return False
    now = time.time()
    # prune
    for k in [k for k, t in _notified.items() if now - t > _NOTIFIED_TTL]:
        _notified.pop(k, None)
    if review_id in _notified:
        return False
    detections = (after.get("data", {}) or {}).get("detections", []) or []
    # Notify as soon as we have a detection (for the GIF), or at the very latest
    # when the review ends.
    if detections or msg_type == "end":
        _notified[review_id] = now
        return True
    return False


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log(f"connected to MQTT {MQTT_HOST}:{MQTT_PORT}, subscribing {TOPIC}")
        client.subscribe(TOPIC)
    else:
        log(f"MQTT connect failed rc={rc}")


def on_message(client, userdata, msg):
    try:
        event = json.loads(msg.payload.decode("utf-8"))
    except Exception as exc:
        log("bad payload:", exc)
        return
    after = event.get("after") or event.get("before") or {}
    msg_type = event.get("type", "")
    if not _should_notify(after, msg_type):
        return
    payload = _build_alert(after)
    if not payload:
        return
    try:
        r = requests.post(f"{RELAY_URL}/v1/notify", json=payload, timeout=10)
        log(f"forwarded review {payload['review_id']} → {r.status_code} {r.text[:120]}")
    except Exception as exc:
        log("relay POST failed:", exc)


def main():
    if not RELAY_URL or not PAIRING_CODE:
        log("FATAL: relay_url and pairing_code are required in the addon options.")
        sys.exit(1)
    if not FRIGATE_BASE_URL:
        log("WARNING: frigate_base_url is empty — notifications will have no image.")

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
