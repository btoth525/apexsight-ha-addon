"""Doorbell talkback glue: config + saved-clip storage on top of ``aqara_talk``.

The relay plays audio to the Aqara doorbell speaker — either a clip the app sends right now, or a
saved preset ("soundboard"). Presets live as files in the data volume so they survive restarts.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional

from . import aqara_talk
from .config import DATA_DIR

# Camera LAN IP + playback loudness come from the add-on options (run.sh → env).
DOORBELL_IP = os.environ.get("DOORBELL_IP", "").strip()
try:
    DOORBELL_GAIN = float(os.environ.get("DOORBELL_GAIN", "3.0"))
except ValueError:
    DOORBELL_GAIN = 3.0

CLIPS_DIR = DATA_DIR / "doorbell_clips"
_MANIFEST = CLIPS_DIR / "clips.json"
MAX_CLIP_BYTES = 8 * 1024 * 1024  # 8 MB — a soundboard clip, not a podcast


def is_configured() -> bool:
    return bool(DOORBELL_IP)


def _load_manifest() -> dict:
    try:
        return json.loads(_MANIFEST.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(data: dict) -> None:
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    _MANIFEST.write_text(json.dumps(data))


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48] or f"clip-{int(time.time())}"


def save_clip(name: str, data: bytes, ext: str) -> dict:
    """Persist an uploaded clip as a preset. Returns {slug, name}."""
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    slug = _slugify(name)
    # De-dupe slugs by suffixing if a different name already claims it.
    base, n = slug, 1
    while slug in manifest and manifest[slug].get("name") != name:
        n += 1
        slug = f"{base}-{n}"
    safe_ext = re.sub(r"[^a-z0-9]+", "", ext.lower())[:5] or "bin"
    filename = f"{slug}.{safe_ext}"
    (CLIPS_DIR / filename).write_bytes(data)
    manifest[slug] = {"name": name, "filename": filename}
    _save_manifest(manifest)
    return {"slug": slug, "name": name}


def list_clips() -> List[dict]:
    return [{"slug": slug, "name": meta.get("name", slug)}
            for slug, meta in sorted(_load_manifest().items(), key=lambda kv: kv[1].get("name", ""))]


def clip_file(slug: str) -> Optional[Path]:
    meta = _load_manifest().get(slug)
    if not meta:
        return None
    path = CLIPS_DIR / meta["filename"]
    return path if path.exists() else None


def delete_clip(slug: str) -> bool:
    manifest = _load_manifest()
    meta = manifest.pop(slug, None)
    if not meta:
        return False
    try:
        (CLIPS_DIR / meta["filename"]).unlink(missing_ok=True)
    except OSError:
        pass
    _save_manifest(manifest)
    return True


def play_path(path: Path) -> int:
    """Blocking — play a local audio file to the doorbell speaker. Run in a threadpool."""
    if not DOORBELL_IP:
        raise aqara_talk.TalkbackError("doorbell_ip is not set in the add-on configuration")
    return aqara_talk.play_audio(
        DOORBELL_IP,
        ["-re", "-i", str(path)],
        volume_gain=DOORBELL_GAIN,
        log=lambda m: print(m, flush=True),
    )


def reachable() -> bool:
    return bool(DOORBELL_IP) and aqara_talk.probe(DOORBELL_IP)
