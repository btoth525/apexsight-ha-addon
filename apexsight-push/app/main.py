"""ApexSight push relay — FastAPI app.

Public API (called by the iOS app + the Home Assistant bridge addon):
  POST /v1/register    — an iPhone registers its APNs device token + pairing code
  POST /v1/unregister  — remove a device token
  POST /v1/notify      — a household's HA bridge forwards a Frigate alert
  GET  /healthz        — liveness probe

Admin web GUI (browser, password-protected): mounted at /admin — upload the
.p8, set Key/Team/Bundle IDs, view devices, send a test push.
"""
import asyncio
import datetime
import json
import os
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import apns, aqara_talk, config, db, doorbell, gate, recap, render
from .admin import router as admin_router

# Read from the add-on env (run.sh) — used by the daily-recap scheduler.
PAIRING_CODE = os.environ.get("PAIRING_CODE", "").upper().strip()

app = FastAPI(title="ApexSight Push Relay", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.session_secret(), https_only=False)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
app.include_router(admin_router)


@app.on_event("startup")
async def _startup() -> None:
    db.init()
    asyncio.create_task(_recap_scheduler())


# ---- daily recap scheduler --------------------------------------------------

async def _recap_scheduler() -> None:
    """Once a minute, send the household's daily recap if it's due and not yet sent
    today — so the daily summary arrives reliably even with the app fully closed."""
    while True:
        try:
            await _maybe_send_recap()
        except Exception as exc:
            print("[recap] error:", exc, flush=True)
        await asyncio.sleep(60)


async def _maybe_send_recap() -> None:
    if not PAIRING_CODE or not apns.is_configured():
        return
    raw = db.get_config(f"recap:{PAIRING_CODE}")
    if not raw:
        return
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not cfg.get("enabled"):
        return
    if not db.devices_for(PAIRING_CODE):
        return

    # The app syncs the user's UTC offset (seconds) so we evaluate "their" time
    # without needing a tz database in the container.
    tz = datetime.timezone(datetime.timedelta(seconds=int(cfg.get("tz_offset", 0))))
    now = datetime.datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    if db.get_config(f"recap_sent:{PAIRING_CODE}") == today:
        return
    target = int(cfg.get("hour", 21)) * 60 + int(cfg.get("minute", 0))
    if (now.hour * 60 + now.minute) < target:
        return

    # Build from the events the bridge accumulated off MQTT today (no Frigate query).
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.recap_events_between(PAIRING_CODE, midnight.timestamp(), now.timestamp())
    title, body = recap.format_recap(rows)
    payload = apns.build_payload(title=title, body=body, apex_url="apex://recap")
    await apns.deliver_to_pairing(PAIRING_CODE, payload, collapse_id=f"recap-{today}")
    db.set_config(f"recap_sent:{PAIRING_CODE}", today)
    db.prune_recap_events(midnight.timestamp() - 2 * 86_400)   # keep ~2 days of history
    print(f"[recap] sent daily recap to {PAIRING_CODE} for {today} ({len(rows)} events)", flush=True)


# ---- naive per-IP rate limiting --------------------------------------------

_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = _hits[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= config.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    window.append(now)


# ---- request models ---------------------------------------------------------

class RegisterIn(BaseModel):
    device_token: str = Field(min_length=32)
    pairing_code: str = Field(min_length=4, max_length=64)
    environment: str = "production"      # "production" | "sandbox"
    platform: str = "ios"
    device_name: str = ""                # user-set phone name → per-phone HA entity (see bridge)


class UnregisterIn(BaseModel):
    device_token: str


class RegisterVoIPIn(BaseModel):
    voip_token: str = Field(min_length=32)
    pairing_code: str = Field(min_length=4, max_length=64)
    environment: str = "production"


class DoorbellRingIn(BaseModel):
    pairing_code: str = ""
    camera: str = "doorbell"


class TestIn(BaseModel):
    device_token: str
    environment: str = "production"


class NotifyIn(BaseModel):
    pairing_code: str
    title: str
    body: str = ""
    camera: str = ""
    review_id: str = ""
    apex_url: str = ""
    snapshot_url: str = ""
    thumbnail_url: str = ""
    snapshot_path: str = ""
    frigate_token: str = ""
    collapse_id: str = ""
    silent: bool = False
    is_description: bool = False   # follow-up carrying the GenAI description; obeys per-camera opt-out
    announce: bool = False   # read aloud via iOS Announce Notifications (CarPlay), no second buzz
    # Raw event fields — when present the relay renders title/body/media itself
    # using the household's saved style (set via /v1/style), so the in-app GUI
    # controls even app-closed notifications.
    camera_name: str = ""
    labels: list[str] = []
    sub_labels: list[str] = []
    zones: list[str] = []
    score: Optional[float] = None
    severity: str = ""
    detection_id: str = ""
    frigate_base_url: str = ""
    recognized_license_plate: str = ""
    stage: str = ""


class DoorbellPlayIn(BaseModel):
    pairing_code: str = ""
    slug: str = Field(min_length=1, max_length=64)   # which saved talkback preset to play/delete


class DoorbellUrlIn(BaseModel):
    pairing_code: str = ""
    url: str = Field(min_length=8, max_length=2048)  # http(s)/rtsp audio URL to play at the door


class AICamerasIn(BaseModel):
    pairing_code: str
    disabled: list[str] = []   # cameras the user turned AI descriptions OFF for (default: all on)


class MutedCamerasIn(BaseModel):
    pairing_code: str
    muted: list[str] = []   # cameras the user turned notifications OFF for entirely (default: all on)


class DevicePrefsIn(BaseModel):
    device_token: str = Field(min_length=8)
    pairing_code: str = ""
    device_name: str = ""   # optional; keeps the per-phone HA entity name fresh on the 15s foreground sync
    # Soft-only notification prefs for THIS device: cameras_disabled / objects_disabled /
    # zones_disabled / camera_snoozes / quiet_hours{enabled,start,end} / tz_offset / triggers.
    # NEVER disarmed or global snoozed_until — those stay real-time household state (see gate.py).
    prefs: dict = {}


class ModeIn(BaseModel):
    # The house mode (home / night / away) — synced from HA on Alarmo state change. Drives the
    # house-level camera filter in /v1/notify (see gate.MODE_MUTES).
    mode: str = ""
    pairing_code: str = ""


class SetModeIn(BaseModel):
    # The app REQUESTS a house mode change (arm/disarm). Routed to HA (which arms Alarmo) via the
    # bridge. Arming rides the pairing code; DISARM (mode=home) must carry the Alarmo `code`, which
    # HA/Alarmo validates server-side — so a leaked pairing code alone can never drop the alarm.
    mode: str
    device_token: str = ""
    pairing_code: str = ""
    code: str = ""   # Alarmo code; required to disarm. Forwarded to HA→Alarmo, never validated here.


class StyleIn(BaseModel):
    pairing_code: str
    style: dict


class GateIn(BaseModel):
    pairing_code: str
    disarmed: bool = False
    snoozed_until: float = 0.0   # epoch seconds; 0 = not snoozed


class RecapIn(BaseModel):
    pairing_code: str
    enabled: bool = False
    hour: int = 21
    minute: int = 0
    tz_offset: int = 0           # seconds from GMT, so the relay sends at the user's local time


# ---- public API -------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "apns_configured": apns.is_configured(), "devices": db.device_count()}


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/admin")


@app.post("/v1/register")
def register(body: RegisterIn, _: None = Depends(rate_limit)) -> dict:
    env = body.environment if body.environment in ("production", "sandbox") else "production"
    db.upsert_device(
        body.device_token, body.pairing_code.upper().strip(), env, body.platform,
        device_name=(body.device_name or "").strip()[:64],
    )
    return {"ok": True, "pairing_code": body.pairing_code.upper().strip()}


@app.post("/v1/unregister")
def unregister(body: UnregisterIn, _: None = Depends(rate_limit)) -> dict:
    db.delete_device(body.device_token)
    return {"ok": True}


@app.post("/v1/register-voip")
def register_voip(body: RegisterVoIPIn, _: None = Depends(rate_limit)) -> dict:
    """An iPhone registers its PushKit VoIP token so the relay can ring it (CallKit) on a doorbell
    press. Separate from the APNs token — VoIP pushes use a different topic + push type."""
    env = body.environment if body.environment in ("production", "sandbox") else "production"
    db.upsert_voip(body.voip_token, body.pairing_code.upper().strip(), env)
    return {"ok": True}


@app.post("/v1/doorbell-ring")
async def doorbell_ring(body: DoorbellRingIn, _: None = Depends(rate_limit)) -> dict:
    """The HA bridge calls this when the doorbell button is pressed. Sends a VoIP push to every
    registered phone in the household so CallKit rings them with the live doorbell call."""
    if not apns.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured on relay")
    code = body.pairing_code.upper().strip() or PAIRING_CODE
    rows = db.voip_tokens_for(code)
    payload = {"aps": {"content-available": 1}, "doorbell": True, "camera": body.camera or "doorbell"}
    sent, failed = 0, 0
    for row in rows:
        ok, detail = await apns.send_voip(row["voip_token"], row["environment"], payload)
        if ok:
            sent += 1
        else:
            failed += 1
            if any(k in detail for k in ("410", "BadDeviceToken", "Unregistered")):
                db.delete_voip(row["voip_token"])
    print(f"[doorbell] ring → {sent} phones (failed {failed})", flush=True)
    return {"ok": True, "sent": sent, "failed": failed, "phones": len(rows)}


# ---- doorbell talkback (play audio to the Aqara speaker) ---------------------

def _require_pairing(code: str) -> str:
    """Talkback actuates hardware (the door speaker), so require the household pairing code."""
    code = (code or "").upper().strip()
    if PAIRING_CODE and code != PAIRING_CODE:
        raise HTTPException(status_code=403, detail="bad pairing code")
    return code


async def _play_upload(upload: UploadFile) -> int:
    """Save an uploaded clip to a temp file and play it to the doorbell. Returns frames sent."""
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(data) > doorbell.MAX_CLIP_BYTES:
        raise HTTPException(status_code=413, detail="clip too large")
    suffix = os.path.splitext(upload.filename or "")[1][:8] or ".bin"
    tmp = tempfile.NamedTemporaryFile(prefix="apex_talk_", suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        return await run_in_threadpool(doorbell.play_path, Path(tmp.name))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.get("/v1/doorbell/status")
async def doorbell_status(pairing_code: str = "", _: None = Depends(rate_limit)) -> dict:
    """Whether talkback is configured + the doorbell is reachable (powers the app's Talk UI)."""
    _require_pairing(pairing_code)
    configured = doorbell.is_configured()
    return {"ok": True, "configured": configured,
            "reachable": (await run_in_threadpool(doorbell.reachable)) if configured else False}


@app.post("/v1/doorbell/clip")
async def doorbell_clip(
    pairing_code: str = Form(""),
    save_as: str = Form(""),
    audio: UploadFile = File(...),
    _: None = Depends(rate_limit),
) -> dict:
    """Play an uploaded clip to the doorbell speaker right now; optionally save it as a preset."""
    _require_pairing(pairing_code)
    if not doorbell.is_configured():
        raise HTTPException(status_code=503, detail="doorbell_ip not set in add-on config")
    saved = None
    if save_as.strip():
        data = await audio.read()
        await audio.seek(0)
        ext = os.path.splitext(audio.filename or "")[1].lstrip(".") or "bin"
        saved = doorbell.save_clip(save_as.strip(), data, ext)
    try:
        frames = await _play_upload(audio)
    except aqara_talk.TalkbackError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "frames": frames, "saved": saved}


@app.get("/v1/doorbell/clips")
async def doorbell_clips(pairing_code: str = "", _: None = Depends(rate_limit)) -> dict:
    _require_pairing(pairing_code)
    return {"ok": True, "clips": doorbell.list_clips()}


@app.post("/v1/doorbell/play")
async def doorbell_play(body: DoorbellPlayIn, _: None = Depends(rate_limit)) -> dict:
    """Play a saved preset by slug."""
    _require_pairing(body.pairing_code)
    if not doorbell.is_configured():
        raise HTTPException(status_code=503, detail="doorbell_ip not set in add-on config")
    path = doorbell.clip_file(body.slug)
    if not path:
        raise HTTPException(status_code=404, detail="clip not found")
    try:
        frames = await run_in_threadpool(doorbell.play_path, path)
    except aqara_talk.TalkbackError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "frames": frames}


@app.post("/v1/doorbell/delete")
async def doorbell_delete(body: DoorbellPlayIn, _: None = Depends(rate_limit)) -> dict:
    _require_pairing(body.pairing_code)
    return {"ok": True, "deleted": doorbell.delete_clip(body.slug)}


@app.post("/v1/doorbell/play-url")
async def doorbell_play_url(body: DoorbellUrlIn, _: None = Depends(rate_limit)) -> dict:
    """Play audio from an http(s)/rtsp URL to the doorbell speaker. This is what the HA bridge
    calls for `apexsight/doorbell/play_url` — so any HA TTS/media URL can speak at the door."""
    _require_pairing(body.pairing_code)
    if not doorbell.is_configured():
        raise HTTPException(status_code=503, detail="doorbell_ip not set in add-on config")
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://", "rtsp://")):
        raise HTTPException(status_code=400, detail="url must be http(s) or rtsp")
    try:
        frames = await run_in_threadpool(
            lambda: aqara_talk.play_audio(
                doorbell.DOORBELL_IP, ["-re", "-i", url],
                volume_gain=doorbell.DOORBELL_GAIN, log=lambda m: print(m, flush=True),
            )
        )
    except aqara_talk.TalkbackError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "frames": frames}


@app.post("/v1/test")
async def test_push(body: TestIn, _: None = Depends(rate_limit)) -> dict:
    """Send a test push to one device token — powers the in-app Test button."""
    if not apns.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured on relay")
    env = body.environment if body.environment in ("production", "sandbox") else "production"
    payload = apns.build_payload(
        title="\U0001f6a8 ApexSight test alert",
        body="Instant push is working — you'll get alerts with the app closed.",
        camera="relay_test",
        review_id="relay-test",
        apex_url="apex://review?id=relay-test",
    )
    ok, detail = await apns.send_to_token(body.device_token, env, payload)
    if not ok:
        raise HTTPException(status_code=502, detail=detail)
    return {"ok": True}


@app.post("/v1/notify")
async def notify(body: NotifyIn, _: None = Depends(rate_limit)) -> dict:
    if not apns.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured on relay")
    code = body.pairing_code.upper().strip()
    if not db.devices_for(code):
        # Nothing registered under this code yet — not an error the bridge should retry on.
        return {"ok": True, "devices": 0, "sent": 0, "note": "no devices for pairing code"}

    # Household HARD gate — Disarm / global Snooze-all. Set in REAL TIME from six contexts (the app,
    # a widget, Siri, the watch, CarPlay, a Focus) via /v1/gate, so it stays the household source of
    # truth for these two and suppresses for ALL devices at once. (Per-device SOFT prefs are applied
    # below in the delivery loop; disarm/global-snooze deliberately are NOT per-device — see the
    # soft-only note in gate.py.)
    gate_raw = db.get_config(f"gate:{code}")
    if gate_raw:
        try:
            g = json.loads(gate_raw)
        except json.JSONDecodeError:
            g = {}
        if g.get("disarmed"):
            return {"ok": True, "sent": 0, "note": "disarmed"}
        snoozed_until = g.get("snoozed_until") or 0
        if snoozed_until and time.time() < float(snoozed_until):
            return {"ok": True, "sent": 0, "note": "snoozed"}

    # House mode camera filter — the Alarmo house mode (Home / Night / Away), synced from HA via
    # /v1/mode, silences camera-detection pushes for cameras that mode mutes (e.g. inside cams while
    # Home). House-level (all devices), applied before the per-device loop. FAIL-OPEN: unknown/blank
    # mode or a camera not in that mode's mute-list delivers; Away mutes nothing.
    house_mode = db.get_config("house_mode", "") or ""
    if gate.mode_mutes_camera(house_mode, body.camera):
        print(f"[mode] {house_mode}: {body.camera} muted → suppress all", flush=True)
        return {"ok": True, "sent": 0, "note": f"mode {house_mode}: camera muted"}

    # (Per-camera mute is no longer a household early-return — it now rides inside the per-device
    # soft gate below, so a trigger can re-open it exactly as in the app. The household
    # muted_cameras:{code} list remains only as a fallback for devices that predate /v1/device-prefs.)

    # Per-camera choice (synced from the app): skip AI-description follow-ups for disabled cameras.
    if body.is_description and body.camera:
        raw = db.get_config(f"ai_desc_disabled:{code}", "")
        if raw:
            try:
                disabled = set(json.loads(raw))
            except Exception:
                disabled = set()
            if body.camera in disabled:
                return {"ok": True, "sent": 0, "note": "ai description disabled for camera"}

    title, text = body.title, body.body
    snapshot_url, thumbnail_url = body.snapshot_url, body.thumbnail_url

    # Raw event present → render here using the household's saved style (or defaults),
    # so the app's GUI controls the content even when the app is closed.
    if body.detection_id or body.labels or body.sub_labels:
        raw = db.get_config(f"style:{code}")
        style = {}
        if raw:
            try:
                style = json.loads(raw)
            except json.JSONDecodeError:
                style = {}
        rendered = render.render(
            {
                "camera": body.camera,
                "camera_name": body.camera_name,
                "labels": body.labels,
                "sub_labels": body.sub_labels,
                "zones": body.zones,
                "score": body.score,
                "severity": body.severity,
                "detection_id": body.detection_id,
                "frigate_base_url": body.frigate_base_url,
                "recognized_license_plate": body.recognized_license_plate,
            },
            style,
            body.stage or "alert",
        )
        title = rendered["title"] or title
        text = rendered["body"] or text
        snapshot_url = rendered["snapshot_url"] or snapshot_url
        thumbnail_url = rendered["thumbnail_url"] or thumbnail_url

    payload = apns.build_payload(
        title=title,
        body=text,
        camera=body.camera,
        review_id=body.review_id,
        apex_url=body.apex_url,
        snapshot_url=snapshot_url,
        thumbnail_url=thumbnail_url,
        snapshot_path=body.snapshot_path,
        frigate_token=body.frigate_token,
        silent=body.silent,
        announce=body.announce,
    )
    # Per-device SOFT gate: camera / object / zone / quiet-hours / per-camera-snooze / triggers,
    # each synced per device via /v1/device-prefs and evaluated FAIL-OPEN (see gate.py). The app
    # gates on the review's first object (AppState.handleReview uses `objects.first`), so mirror
    # that here. Disarm + global snooze were already handled household-wide above.
    ev_label = body.labels[0] if body.labels else "object"
    ev_zones = body.zones or []
    ev_score = body.score or 0.0
    now = time.time()

    def device_gate(token: str) -> tuple[bool, str]:
        prefs_raw = db.get_config(f"prefs:{token}")
        if prefs_raw:
            try:
                return gate.would_deliver(
                    json.loads(prefs_raw), body.camera, ev_label, ev_zones, ev_score, now
                )
            except Exception as exc:  # noqa: BLE001 — FAIL-OPEN: never suppress on a parse error
                return True, f"fail-open: prefs parse {exc!r}"
        # Older app that hasn't synced per-device prefs yet → household per-camera mute fallback.
        if body.camera:
            raw = db.get_config(f"muted_cameras:{code}", "")
            if raw:
                try:
                    if body.camera in set(json.loads(raw)):
                        return False, "household camera muted"
                except Exception:
                    pass
        return True, "no per-device prefs"

    result = await apns.deliver_to_pairing(
        code, payload, collapse_id=body.collapse_id, gate=device_gate
    )
    return {"ok": result["sent"] > 0 or result["devices"] == 0, **result}


@app.post("/v1/ai-cameras")
def ai_cameras(body: AICamerasIn, _: None = Depends(rate_limit)) -> dict:
    """The app syncs which cameras have AI descriptions in notifications turned OFF, per household."""
    code = body.pairing_code.upper().strip()
    db.set_config(f"ai_desc_disabled:{code}", json.dumps(sorted(set(body.disabled))))
    return {"ok": True, "disabled": sorted(set(body.disabled))}


@app.post("/v1/muted-cameras")
def muted_cameras(body: MutedCamerasIn, _: None = Depends(rate_limit)) -> dict:
    """The app syncs which cameras have notifications turned OFF entirely, per household, so
    app-closed pushes for a muted camera are suppressed at the relay (the in-app per-camera
    toggle otherwise only gates foreground delivery)."""
    code = body.pairing_code.upper().strip()
    db.set_config(f"muted_cameras:{code}", json.dumps(sorted(set(body.muted))))
    return {"ok": True, "muted": sorted(set(body.muted))}


@app.post("/v1/device-prefs")
def device_prefs(body: DevicePrefsIn, _: None = Depends(rate_limit)) -> dict:
    """Each device syncs its OWN soft notification prefs here (per-camera/object/zone mutes, quiet
    hours, per-camera snoozes, triggers), keyed by device token, so app-closed pushes are gated per
    device exactly as the foreground app would. Fired on any settings change and on foreground."""
    token = body.device_token.strip()
    prefs = dict(body.prefs or {})
    # Defense in depth for the soft-only invariant (see gate.py): even if an app build ever includes
    # them, strip Disarm / global Snooze — a stale copy here would suppress live alerts after a
    # household re-arm from a widget/Siri. Those two stay real-time household state via /v1/gate.
    prefs.pop("disarmed", None)
    prefs.pop("snoozed_until", None)
    db.set_config(f"prefs:{token}", json.dumps(prefs))
    # Keep the per-phone HA entity name fresh (and bump updated_at → "last seen") without needing a
    # full re-register. Name-only update — never touches environment/pairing (see db.set_device_name).
    name = (body.device_name or "").strip()[:64]
    if name:
        db.set_device_name(token, name)
    return {"ok": True}


@app.post("/v1/mode")
def set_mode(body: ModeIn, _: None = Depends(rate_limit)) -> dict:
    """HA syncs the house mode (home / night / away) here on every Alarmo state change. It drives
    the house-level camera filter in /v1/notify. Only a known mode is stored; an unrecognized value
    is ignored so the gate keeps failing open (mode unknown → deliver) rather than muting blindly."""
    mode = (body.mode or "").strip().lower()
    if mode in gate.MODE_MUTES:
        db.set_config("house_mode", mode)
        return {"ok": True, "mode": mode}
    return {"ok": False, "note": f"unknown mode '{mode}' ignored (fail-open)"}


@app.get("/v1/mode")
def get_mode() -> dict:
    """Current house mode + which cameras it mutes — for the app (to reflect state), the HA bridge,
    and debugging. `armed_by` reports who last requested a change (from the app), for display."""
    mode = db.get_config("house_mode", "") or ""
    armed_by = {}
    raw = db.get_config("armed_by", "")
    if raw:
        try:
            armed_by = json.loads(raw)
        except json.JSONDecodeError:
            armed_by = {}
    return {"mode": mode, "mutes": gate.MODE_MUTES.get(mode, []), "armed_by": armed_by}


@app.post("/v1/set-mode")
def set_mode_request(body: SetModeIn, _: None = Depends(rate_limit)) -> dict:
    """The app requests an arm/disarm. We persist the request (consume-once via a monotonic seq) for
    the bridge to publish to HA over MQTT; HA arms Alarmo, whose resulting state flows back through
    `apexsight/mode` → house_mode → the app + cameras. SECURITY: disarm (mode=home) must carry the
    Alarmo `code` — validated by Alarmo itself, so the public pairing code alone can't disarm."""
    mode = (body.mode or "").strip().lower()
    if mode not in ("home", "away", "night"):
        raise HTTPException(status_code=400, detail="unknown mode")
    # Disarm requires the Alarmo code in the request (HA/Alarmo does the actual validation). Reject a
    # code-less disarm here so a bare pairing-code POST can't even form one.
    if mode == "home" and not (body.code or "").strip():
        raise HTTPException(status_code=403, detail="disarm requires the alarm code")

    by = db.device_name_for(body.device_token.strip()) if body.device_token.strip() else ""
    seq = int(db.get_config("mode_request_seq", "0") or "0") + 1
    now = time.time()
    db.set_config("mode_request", json.dumps(
        {"seq": seq, "mode": mode, "by": by, "code": (body.code or "").strip(), "ts": now}
    ))
    db.set_config("mode_request_seq", str(seq))
    # Record who requested it for the who-armed sensor (the app + HA show this).
    db.set_config("armed_by", json.dumps({"by": by, "mode": mode, "ts": now}))
    # Never log the code.
    print(f"[set-mode] request seq={seq} mode={mode} by={by or '?'}", flush=True)
    return {"ok": True, "seq": seq, "mode": mode, "by": by}


@app.post("/v1/style")
def set_style(body: StyleIn, _: None = Depends(rate_limit)) -> dict:
    """The iOS app saves its notification style here, keyed by pairing code, so the
    relay can render app-closed pushes the way the user configured in the GUI."""
    code = body.pairing_code.upper().strip()
    db.set_config(f"style:{code}", json.dumps(body.style))
    return {"ok": True}


@app.post("/v1/gate")
def set_gate(body: GateIn, _: None = Depends(rate_limit)) -> dict:
    """The iOS app mirrors its Disarm / Snooze state here so app-closed pushes are
    suppressed while disarmed or snoozed, matching the in-app delivery gate."""
    code = body.pairing_code.upper().strip()
    db.set_config(
        f"gate:{code}",
        json.dumps({"disarmed": bool(body.disarmed), "snoozed_until": float(body.snoozed_until or 0)}),
    )
    return {"ok": True}


@app.post("/v1/recap")
def set_recap(body: RecapIn, _: None = Depends(rate_limit)) -> dict:
    """The iOS app saves its Daily Recap schedule here so the relay can send the
    summary at the chosen local time even when the app is fully closed."""
    code = body.pairing_code.upper().strip()
    db.set_config(
        f"recap:{code}",
        json.dumps({
            "enabled": bool(body.enabled),
            "hour": int(body.hour),
            "minute": int(body.minute),
            "tz_offset": int(body.tz_offset),
        }),
    )
    return {"ok": True}
