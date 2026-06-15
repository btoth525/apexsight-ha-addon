"""Password-protected admin web GUI.

Lets you (the relay owner) upload your APNs .p8, set the Key/Team/Bundle IDs,
review registered devices, prune stale ones, and fire a test push — all from a
browser, no command line. Auth is a single shared password (APEX_ADMIN_PASSWORD)
held in a signed session cookie.
"""
import hmac
import os
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import apns, config, db

router = APIRouter(prefix="/admin")
_templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# ---- brute-force lockout for the admin login (fail2ban-style) ---------------
# After MAX_FAILS bad attempts from one IP within FAIL_WINDOW seconds, that IP is
# blocked from logging in for LOCKOUT seconds. Successful login clears the count.
MAX_FAILS = 5
FAIL_WINDOW = 15 * 60
LOCKOUT = 15 * 60
_fails: dict[str, deque] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def _client_ip(request: Request) -> str:
    # Behind a tunnel/proxy the socket peer is the proxy, so prefer the real
    # client IP from the standard forwarding headers.
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _lock_remaining(ip: str) -> int:
    until = _locked_until.get(ip, 0)
    return max(0, int(until - time.time()))


def _record_failure(ip: str) -> None:
    now = time.time()
    window = _fails[ip]
    window.append(now)
    while window and now - window[0] > FAIL_WINDOW:
        window.popleft()
    if len(window) >= MAX_FAILS:
        _locked_until[ip] = now + LOCKOUT
        window.clear()


def _clear_failures(ip: str) -> None:
    _fails.pop(ip, None)
    _locked_until.pop(ip, None)


def _authed(request: Request) -> bool:
    return bool(request.session.get("admin"))


def _require(request: Request) -> None:
    if not config.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin UI disabled: set APEX_ADMIN_PASSWORD.")
    if not _authed(request):
        raise HTTPException(status_code=307, detail="login", headers={"Location": "/admin/login"})


def _settings() -> dict:
    cfg = db.all_config()
    return {
        "key_id": cfg.get("apns_key_id", ""),
        "team_id": cfg.get("apns_team_id", config.DEFAULT_TEAM_ID),
        "bundle_id": cfg.get("apns_bundle_id", config.DEFAULT_BUNDLE_ID),
        "env_mode": cfg.get("apns_env_mode", "auto"),
        "p8_loaded": bool(cfg.get("apns_p8")),
    }


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return _templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not config.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin UI disabled: set APEX_ADMIN_PASSWORD.")

    ip = _client_ip(request)
    remaining = _lock_remaining(ip)
    if remaining > 0:
        return _templates.TemplateResponse(
            "login.html",
            {"request": request, "error": f"Too many attempts. Try again in {remaining // 60 + 1} min."},
            status_code=429,
        )

    # Evaluate both with constant-time compares, then AND (no short-circuit).
    user_ok = hmac.compare_digest(username.strip(), config.ADMIN_USERNAME)
    pass_ok = hmac.compare_digest(password, config.ADMIN_PASSWORD)
    if user_ok and pass_ok:
        _clear_failures(ip)
        request.session["admin"] = True
        return RedirectResponse(url="/admin", status_code=303)

    _record_failure(ip)
    return _templates.TemplateResponse(
        "login.html", {"request": request, "error": "Wrong username or password."}, status_code=401
    )


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request):
    _require(request)
    return _templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "settings": _settings(),
            "configured": apns.is_configured(),
            "device_count": db.device_count(),
            "flash": request.session.pop("flash", None),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    _require(request)
    return _templates.TemplateResponse(
        "settings.html", {"request": request, "settings": _settings(), "flash": request.session.pop("flash", None)}
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    key_id: str = Form(""),
    team_id: str = Form(""),
    bundle_id: str = Form(""),
    env_mode: str = Form("auto"),
    p8: UploadFile | None = None,
):
    _require(request)
    if key_id:
        db.set_config("apns_key_id", key_id.strip())
    if team_id:
        db.set_config("apns_team_id", team_id.strip())
    if bundle_id:
        db.set_config("apns_bundle_id", bundle_id.strip())
    if env_mode in ("auto", "production", "sandbox"):
        db.set_config("apns_env_mode", env_mode)
    if p8 is not None and p8.filename:
        raw = (await p8.read()).decode("utf-8", errors="ignore").strip()
        if "BEGIN PRIVATE KEY" not in raw:
            request.session["flash"] = "That file doesn't look like a .p8 private key — not saved."
            return RedirectResponse(url="/admin/settings", status_code=303)
        db.set_config("apns_p8", raw)
        request.session["flash"] = "Saved. APNs key uploaded ✓"
    else:
        request.session["flash"] = "Settings saved."
    return RedirectResponse(url="/admin/settings", status_code=303)


@router.get("/devices", response_class=HTMLResponse)
def devices(request: Request):
    _require(request)
    rows = db.all_devices()
    # Group by pairing code for a household-centric view.
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r["pairing_code"], []).append(r)
    return _templates.TemplateResponse(
        "devices.html", {"request": request, "grouped": grouped, "flash": request.session.pop("flash", None)}
    )


@router.post("/devices/delete")
def delete_device(request: Request, device_token: str = Form(...)):
    _require(request)
    db.delete_device(device_token)
    request.session["flash"] = "Device removed."
    return RedirectResponse(url="/admin/devices", status_code=303)


@router.post("/test")
async def test_push(request: Request, pairing_code: str = Form(...)):
    _require(request)
    if not apns.is_configured():
        request.session["flash"] = "Configure APNs first (upload your .p8 + IDs)."
        return RedirectResponse(url="/admin", status_code=303)
    payload = apns.build_payload(
        title="\U0001f6a8 ApexSight test alert",
        body="If you can read this on your phone, your relay works.",
        camera="relay_test",
        review_id="relay-test",
        apex_url="apex://review?id=relay-test",
    )
    result = await apns.deliver_to_pairing(pairing_code.upper().strip(), payload)
    request.session["flash"] = (
        f"Test sent → {result['sent']}/{result['devices']} delivered."
        + (f" Errors: {'; '.join(result['errors'][:3])}" if result["errors"] else "")
    )
    return RedirectResponse(url="/admin", status_code=303)
