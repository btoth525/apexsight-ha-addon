"""Renders an APNs notification (title / body / media) from raw Frigate event
fields plus a per-household style config the iOS app sets via /v1/style.

This is what lets the in-app GUI control even app-closed (instant) notifications:
the HA bridge forwards raw fields, and the relay styles them here. The defaults
mirror the popular SgtBatten Frigate-notifications blueprint.
"""
from __future__ import annotations

from datetime import datetime

DEFAULT_EMOJI = {
    "amazon": "📦", "ups": "📦", "usps": "📮", "fedex": "✈️", "dhl": "📬",
    "face": "🙂", "known_face": "😎", "recognized_face": "😎", "unknown_face": "🤔",
    "license_plate": "🅿️", "plate": "🅿️", "lpr": "🅿️",
    "person": "🚶", "person-verified": "🚶",
    "car": "🚗", "vehicle": "🚗", "truck": "🚚", "motorcycle": "🏍️", "bicycle": "🚲",
    "bus": "🚌", "mail": "📮", "mail carrier": "📮", "package": "📦",
    "dog": "🐕", "cat": "🐈", "bird": "🐦", "deer": "🦌", "bear": "🐻",
    "squirrel": "🐿️", "rabbit": "🐇", "door": "🚪", "doorbell": "🔔", "alert": "🚨",
}

DEFAULT_STYLE = {
    "subLabelFirst": True,
    "severityPrefix": True,
    "showEmojis": True,
    "showEntities": True,
    "showZone": True,
    "showConfidence": True,
    "showTime": True,
    "fieldSeparator": " · ",
    "firstFrame": "cropped",   # cropped | full | none
    "finalGif": True,
}


def _titleize(s: str) -> str:
    return str(s).replace("_", " ").title() if s else ""


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x not in (None, "")]
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()] if "," in v else ([v] if v.strip() else [])
    return [v]


def render(ev: dict, style: dict | None, stage: str) -> dict:
    """ev: camera, camera_name, labels, sub_labels, zones, score, severity,
    detection_id, frigate_base_url. Returns {title, body, snapshot_url, thumbnail_url}."""
    s = {**DEFAULT_STYLE, **(style or {})}
    emoji_map = {**DEFAULT_EMOJI, **(s.get("emojiMap") or {})}
    sep = s.get("fieldSeparator", " · ")

    labels = _as_list(ev.get("labels"))
    subs = _as_list(ev.get("sub_labels"))
    zones = _as_list(ev.get("zones"))
    severity = ev.get("severity") or "detection"
    camera_name = ev.get("camera_name") or _titleize(ev.get("camera", ""))

    keys = [str(x).lower().strip() for x in (subs + labels) if str(x).strip()]

    # ---- title ----
    title_parts: list[str] = []
    if s.get("severityPrefix") and severity == "alert":
        title_parts.append("🚨")
    if s.get("showEmojis"):
        emojis: list[str] = []
        for k in keys:
            e = emoji_map.get(k)
            if e and e not in emojis:
                emojis.append(e)
        title_parts.append(" ".join(emojis) if emojis else "⚠️")
    if camera_name:
        title_parts.append(camera_name)
    title = " ".join(p for p in title_parts if p)

    # ---- body ----
    ordered = (subs + labels) if s.get("subLabelFirst") else (labels + subs)
    ents: list[str] = []
    for x in ordered:
        t = _titleize(x)
        if t and t not in ents:
            ents.append(t)

    fields: list[str] = []
    if s.get("showEntities"):
        fields.append(sep.join(ents) if ents else "Detection")
    if s.get("showZone") and zones:
        fields.append("Zone: " + ", ".join(_titleize(z) for z in zones))
    if s.get("showConfidence") and ev.get("score"):
        try:
            fields.append(f"{round(float(ev['score']) * 100)}%")
        except (TypeError, ValueError):
            pass
    if s.get("showTime"):
        fields.append(datetime.now().strftime("%-I:%M %p"))
    body = sep.join(f for f in fields if f)

    # ---- media (cropped snapshot on the instant alert, GIF on the final update) ----
    base = (ev.get("frigate_base_url") or "").rstrip("/")
    det = ev.get("detection_id")
    snapshot_url = ""
    thumbnail_url = ""
    if base and det:
        cropped = f"{base}/api/events/{det}/snapshot.jpg?bbox=1&crop=1"
        gif = f"{base}/api/events/{det}/preview.gif"
        full = f"{base}/api/events/{det}/snapshot.jpg"
        if stage == "final":
            snapshot_url = gif if s.get("finalGif") else cropped
            thumbnail_url = cropped
        else:
            ff = s.get("firstFrame", "cropped")
            snapshot_url = cropped if ff == "cropped" else (full if ff == "full" else "")
            thumbnail_url = full

    return {"title": title, "body": body, "snapshot_url": snapshot_url, "thumbnail_url": thumbnail_url}
