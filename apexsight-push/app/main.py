"""ApexSight push relay — FastAPI app.

Public API (called by the iOS app + the Home Assistant bridge addon):
  POST /v1/register    — an iPhone registers its APNs device token + pairing code
  POST /v1/unregister  — remove a device token
  POST /v1/notify      — a household's HA bridge forwards a Frigate alert
  GET  /healthz        — liveness probe

Admin web GUI (browser, password-protected): mounted at /admin — upload the
.p8, set Key/Team/Bundle IDs, view devices, send a test push.
"""
import os
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import apns, config, db
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
    payload = apns.build_payload(
        title=body.title,
        body=body.body,
        camera=body.camera,
        review_id=body.review_id,
        apex_url=body.apex_url,
        snapshot_url=body.snapshot_url,
        thumbnail_url=body.thumbnail_url,
        snapshot_path=body.snapshot_path,
        frigate_token=body.frigate_token,
    )
    result = await apns.deliver_to_pairing(code, payload)
    return {"ok": result["sent"] > 0 or result["devices"] == 0, **result}
