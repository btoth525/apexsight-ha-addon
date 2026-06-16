"""ApexSight push relay — FastAPI app.

Public API (called by the iOS app + the Home Assistant bridge addon):
  POST /v1/register    — an iPhone registers its APNs device token + pairing code
  POST /v1/unregister  — remove a device token
  POST /v1/notify      — a household's HA bridge forwards a Frigate alert
  GET  /healthz        — liveness probe

Admin web GUI (browser, password-protected): mounted at /admin — upload the
.p8, set Key/Team/Bundle IDs, view devices, send a test push.
"""
import json
import os
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import apns, config, db, render
from .admin import router as admin_router

app = FastAPI(title="ApexSight Push Relay", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.session_secret(), https_only=False)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
app.include_router(admin_router)


@app.on_event("startup")
def _startup() -> None:
    db.init()


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


class UnregisterIn(BaseModel):
    device_token: str


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
    stage: str = ""


class StyleIn(BaseModel):
    pairing_code: str
    style: dict


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
    db.upsert_device(body.device_token, body.pairing_code.upper().strip(), env, body.platform)
    return {"ok": True, "pairing_code": body.pairing_code.upper().strip()}


@app.post("/v1/unregister")
def unregister(body: UnregisterIn, _: None = Depends(rate_limit)) -> dict:
    db.delete_device(body.device_token)
    return {"ok": True}


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
    )
    result = await apns.deliver_to_pairing(code, payload, collapse_id=body.collapse_id)
    return {"ok": result["sent"] > 0 or result["devices"] == 0, **result}


@app.post("/v1/style")
def set_style(body: StyleIn, _: None = Depends(rate_limit)) -> dict:
    """The iOS app saves its notification style here, keyed by pairing code, so the
    relay can render app-closed pushes the way the user configured in the GUI."""
    code = body.pairing_code.upper().strip()
    db.set_config(f"style:{code}", json.dumps(body.style))
    return {"ok": True}
