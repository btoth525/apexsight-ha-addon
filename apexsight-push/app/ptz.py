"""Pan/tilt for the Tapo C246D telephoto lenses (1.32.0).

Each C246D is two cameras in one body: a fixed wide lens (`Backyard_Wide`, `Living_Room_Wide`) and a
pan/tilt telephoto lens (`Backyard_PTZ`, `Living_Room_PTZ`). Frigate drives the tele over ONVIF, but
only in four fixed directions at one speed, and it can't aim at a point: these cameras don't offer the
field-of-view relative move Frigate's click-to-center needs. The relay talks ONVIF to the camera
itself so the app can do three things Frigate can't:

  * an ANALOG joystick: any direction, any speed (ContinuousMove with the real velocity vector),
    behind a dead-man lease so a dropped phone connection can never leave the lens panning;
  * tap-to-aim: tap a spot on the WIDE lens and the tele swings there (AbsoluteMove);
  * a live "where it's looking" box on the wide lens, from the tele's reported position.

Aiming rests on a calibration measured on 2026-11-25: the tele was stepped through a grid of absolute
positions and its picture located inside the wide lens's picture by template matching, then fitted
(RANSAC, ~1.6 % of the frame RMS). The fit maps (pan, tilt) in ONVIF's generic space to the wide
lens's normalized (x, y) and the tele's footprint width. Re-measure if a camera is remounted.

Credentials are never stored here: the camera login is read from Frigate's own config (its `onvif:`
block) over the unauthenticated LAN API and cached briefly. Camera names are an allowlist (the
calibration keys), so a request can only ever reach these two cameras.
"""
import asyncio
import base64
import datetime
import hashlib
import json
import os
import re
import time
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

# Coefficients for x = tx(p,t)·cx, y = ty(p,t)·cy, footprint w = [1,(x-.5)²,(y-.5)²]·cs.
# `pan`/`tilt` are the calibrated ranges; aiming clamps to them.
CALIBRATION = {
    "Backyard_PTZ": {
        "wide": "Backyard_Wide",
        "cx": [0.40479, 1.39528, 0.41996, -1.62644, -0.31324, 0.00349],
        "cy": [0.97117, 0.60542, -0.54295, -0.26856, -0.00404, -0.03296],
        "cs": [0.5405, -0.4963, -0.2632],
        "pan": [-0.171, 0.37],
        "tilt": [-1.0, 0.0],
    },
    "Living_Room_PTZ": {
        "wide": "Living_Room_Wide",
        "cx": [0.57749, -1.62766, -0.37269, 1.72402, 0.03682, -0.01627],
        "cy": [0.02235, -0.15757, 1.847, 1.35427, -0.01886, -0.00494],
        "cs": [0.5402, -0.3091, -0.2959],
        "pan": [-0.271, 0.271],
        "tilt": [-1.0, -0.054],
    },
}

# A held joystick re-sends its velocity about every 0.4 s. If nothing arrives for this long the lens
# is stopped: a phone that loses signal mid-pan must not leave the camera sweeping to its end stop.
MOVE_LEASE_SECONDS = 1.2
_CRED_TTL = 300.0
_ONVIF_TIMEOUT = 5.0

_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces"
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
_B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


# ---- calibration math (pure; unit-tested) --------------------------------------------------------

def _tx(p: float, t: float) -> list:
    return [1.0, p, p * p, p ** 3, p * t, t]


def _ty(p: float, t: float) -> list:
    return [1.0, t, t * t, t ** 3, p, p * t]


def _dot(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))


def project(cal: dict, pan: float, tilt: float) -> tuple:
    """Where the tele is looking, in the wide lens's normalized frame: (x, y, w). Both lenses are
    16:9, so the footprint's height fraction equals its width fraction."""
    x = _dot(_tx(pan, tilt), cal["cx"])
    y = _dot(_ty(pan, tilt), cal["cy"])
    w = _dot([1.0, (x - 0.5) ** 2, (y - 0.5) ** 2], cal["cs"])
    return x, y, max(0.05, min(w, 1.0))


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def solve(cal: dict, x: float, y: float) -> tuple:
    """The (pan, tilt) that puts the tele's centre on wide point (x, y), clamped to the calibrated
    range. A coarse grid picks the start (the fit is not globally invertible in closed form), then a
    few damped Gauss-Newton steps refine it. A point the tele can't reach lands on the nearest one."""
    (p0, p1), (t0, t1) = cal["pan"], cal["tilt"]
    best = None
    n = 24
    for i in range(n + 1):
        p = p0 + (p1 - p0) * i / n
        for j in range(n + 1):
            t = t0 + (t1 - t0) * j / n
            px, py, _ = project(cal, p, t)
            err = (px - x) ** 2 + (py - y) ** 2
            if best is None or err < best[0]:
                best = (err, p, t)
    _, p, t = best
    h = 1e-4
    for _ in range(8):
        fx, fy, _ = project(cal, p, t)
        rx, ry = fx - x, fy - y
        if rx * rx + ry * ry < 1e-8:
            break
        ax, ay, _ = project(cal, p + h, t)
        bx, by, _ = project(cal, p, t + h)
        j11, j21 = (ax - fx) / h, (ay - fy) / h
        j12, j22 = (bx - fx) / h, (by - fy) / h
        det = j11 * j22 - j12 * j21
        if abs(det) < 1e-9:
            break
        dp = (j22 * rx - j12 * ry) / det
        dt = (-j21 * rx + j11 * ry) / det
        p = _clamp(p - 0.8 * dp, p0, p1)
        t = _clamp(t - 0.8 * dt, t0, t1)
    return round(p, 4), round(t, 4)


def aim_box(cal: dict, pan: float, tilt: float) -> dict:
    x, y, w = project(cal, pan, tilt)
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(w, 4)}


def clean_preset_name(name: str) -> str:
    """The camera stores the name; keep it to what Tapo accepts and the app can show."""
    name = re.sub(r"[^A-Za-z0-9 _\-']", "", (name or "")).strip()
    return re.sub(r"\s+", " ", name)[:24]


# ---- ONVIF ---------------------------------------------------------------------------------------

def _security(user: str, password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (f'<s:Header><Security s:mustUnderstand="1" xmlns="{_WSSE}"><UsernameToken>'
            f"<Username>{_xml(user)}</Username><Password Type=\"{_DIGEST}\">{digest}</Password>"
            f'<Nonce EncodingType="{_B64}">{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="{_WSU}">{created}</Created></UsernameToken></Security></s:Header>')


def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def _num(v: float) -> str:
    return f"{v:.4f}"


class OnvifError(Exception):
    pass


class Onvif:
    PROFILE = "profile_1"

    def __init__(self, host: str, port: int, user: str, password: str):
        self.url = f"http://{host}:{port}/onvif/service"
        self.user, self.password = user, password

    async def _call(self, body: str) -> str:
        env = (f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
               f"{_security(self.user, self.password)}<s:Body>{body}</s:Body></s:Envelope>")
        try:
            async with httpx.AsyncClient(timeout=_ONVIF_TIMEOUT) as client:
                r = await client.post(self.url, content=env.encode(),
                                      headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        except Exception as exc:
            raise OnvifError(f"camera unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            m = re.search(r"<[^>]*Text[^>]*>([^<]+)<", r.text)
            raise OnvifError(f"camera said {r.status_code}: {m.group(1) if m else 'error'}")
        return r.text

    async def status(self) -> tuple:
        t = await self._call(f'<GetStatus xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                             f"<ProfileToken>{self.PROFILE}</ProfileToken></GetStatus>")
        m = re.search(r'<[^>]*PanTilt[^>]*\bx="([-\d.eE]+)"[^>]*\by="([-\d.eE]+)"', t)
        if not m:
            raise OnvifError("no position in status")
        return float(m.group(1)), float(m.group(2))

    async def continuous(self, vx: float, vy: float) -> None:
        await self._call(f'<ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                         f"<ProfileToken>{self.PROFILE}</ProfileToken><Velocity>"
                         f'<PanTilt x="{_num(vx)}" y="{_num(vy)}" space="{_SPACE}/VelocityGenericSpace" '
                         f'xmlns="http://www.onvif.org/ver10/schema"/></Velocity></ContinuousMove>')

    async def stop(self) -> None:
        await self._call(f'<Stop xmlns="http://www.onvif.org/ver20/ptz/wsdl"><ProfileToken>{self.PROFILE}'
                         f"</ProfileToken><PanTilt>true</PanTilt><Zoom>false</Zoom></Stop>")

    async def absolute(self, pan: float, tilt: float) -> None:
        await self._call(f'<AbsoluteMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                         f"<ProfileToken>{self.PROFILE}</ProfileToken><Position>"
                         f'<PanTilt x="{_num(pan)}" y="{_num(tilt)}" space="{_SPACE}/PositionGenericSpace" '
                         f'xmlns="http://www.onvif.org/ver10/schema"/></Position></AbsoluteMove>')

    async def presets(self) -> list:
        t = await self._call(f'<GetPresets xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                             f"<ProfileToken>{self.PROFILE}</ProfileToken></GetPresets>")
        return parse_presets(t)

    async def set_preset(self, name: str) -> str:
        t = await self._call(f'<SetPreset xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                             f"<ProfileToken>{self.PROFILE}</ProfileToken>"
                             f"<PresetName>{_xml(name)}</PresetName></SetPreset>")
        m = re.search(r"PresetToken>([^<]+)<", t)
        if not m:
            raise OnvifError("camera did not return a preset token")
        return m.group(1)

    async def goto_preset(self, token: str) -> None:
        await self._call(f'<GotoPreset xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                         f"<ProfileToken>{self.PROFILE}</ProfileToken>"
                         f"<PresetToken>{_xml(token)}</PresetToken></GotoPreset>")

    async def remove_preset(self, token: str) -> None:
        await self._call(f'<RemovePreset xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
                         f"<ProfileToken>{self.PROFILE}</ProfileToken>"
                         f"<PresetToken>{_xml(token)}</PresetToken></RemovePreset>")


def parse_presets(xml: str) -> list:
    out = []
    for m in re.finditer(r'<[^>]*:Preset\b[^>]*\btoken="([^"]+)"[^>]*>(.*?)</[^>]*:Preset>', xml, re.S):
        name = re.search(r"<[^>]*:Name>([^<]*)<", m.group(2))
        out.append({"token": m.group(1), "name": (name.group(1) if name else m.group(1)).strip()})
    return out


# ---- camera registry -----------------------------------------------------------------------------

def frigate_api_url() -> str:
    explicit = (os.environ.get("FRIGATE_API_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    host = (os.environ.get("FRIGATE_RTSP_HOST") or "").strip().split(":", 1)[0]
    return f"http://{host}:5000" if host else ""


_creds: dict = {"at": 0.0, "cams": {}}
_creds_lock = asyncio.Lock()


def _unquote(v: str) -> str:
    v = v.split(" #", 1)[0].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v


def parse_onvif_block(raw: str, name: str) -> Optional[dict]:
    """The `onvif:` block of camera `name` from Frigate's raw YAML. `/api/config` disguises the
    password (it returns a stand-in, not the secret), so the raw file is the only place the relay can
    read it. Only the four scalar keys Frigate's own block uses are taken; nothing else is parsed."""
    lines = raw.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.rstrip() == f"  {name}:")
    except StopIteration:
        return None
    block = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("   ") and not line.lstrip().startswith("#"):
            break
        block.append(line)
    try:
        o = next(i for i, l in enumerate(block) if l.rstrip() == "    onvif:")
    except StopIteration:
        return None
    out = {}
    for line in block[o + 1:]:
        if line.strip() and not line.startswith("      "):
            break
        m = re.match(r"^      (host|port|user|password):\s*(.*)$", line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out if out.get("host") and out.get("user") and out.get("password") else None


async def _load_onvif_blocks() -> dict:
    base = frigate_api_url()
    if not base:
        raise HTTPException(status_code=503, detail="relay has no Frigate LAN address (frigate_rtsp_host)")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{base}/api/config/raw")
        # Frigate 0.18 sends the YAML as a JSON-encoded string under a text/plain content type.
        raw = r.text
        if raw[:1] == '"':
            raw = json.loads(raw)
        if not isinstance(raw, str):
            raise ValueError("unexpected config payload")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"couldn't read Frigate config: {type(exc).__name__}")
    out = {}
    for name in CALIBRATION:
        o = parse_onvif_block(raw, name)
        if o:
            out[name] = Onvif(o["host"], int(o.get("port") or 80), o["user"], o["password"])
    return out


async def camera(name: str) -> Onvif:
    if name not in CALIBRATION:
        raise HTTPException(status_code=404, detail="not a pan/tilt camera")
    async with _creds_lock:
        if time.time() - _creds["at"] > _CRED_TTL or name not in _creds["cams"]:
            _creds["cams"] = await _load_onvif_blocks()
            _creds["at"] = time.time()
    cam = _creds["cams"].get(name)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"{name} has no onvif block in Frigate")
    return cam


# ---- dead-man lease for continuous moves ---------------------------------------------------------

_lease_tasks: dict = {}


def _arm_lease(name: str, cam: Onvif) -> None:
    old = _lease_tasks.pop(name, None)
    if old:
        old.cancel()

    async def expire():
        try:
            await asyncio.sleep(MOVE_LEASE_SECONDS)
        except asyncio.CancelledError:
            return
        _lease_tasks.pop(name, None)
        try:
            await cam.stop()
            print(f"[ptz] {name}: joystick went quiet, lens stopped", flush=True)
        except OnvifError as exc:
            print(f"[ptz] {name}: lease stop failed: {exc}", flush=True)

    _lease_tasks[name] = asyncio.create_task(expire())


def _disarm_lease(name: str) -> None:
    old = _lease_tasks.pop(name, None)
    if old:
        old.cancel()


# ---- routes --------------------------------------------------------------------------------------

class MoveIn(BaseModel):
    pairing_code: str = ""
    x: float = 0.0      # pan velocity, -1 (left) … 1 (right)
    y: float = 0.0      # tilt velocity, -1 (down) … 1 (up)


class AimIn(BaseModel):
    pairing_code: str = ""
    x: float            # wide-lens point, 0…1 from the left
    y: float            # wide-lens point, 0…1 from the top


class PresetIn(BaseModel):
    pairing_code: str = ""
    name: str = ""


class CodeIn(BaseModel):
    pairing_code: str = ""


def _bad_gateway(exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


def build_router(require_pairing: Callable[[str], str], rate_limit: Callable,
                 move_rate_limit: Callable) -> APIRouter:
    """Routes are built by main.py so they share its pairing gate and rate limiters (importing main
    from here would be circular). Joystick updates get their own, roomier limiter: a held stick sends
    a few a second, which the general 120/min would cut off mid-pan."""
    router = APIRouter(prefix="/v1/ptz")

    @router.get("")
    async def list_cameras(pairing_code: str = "", _: None = Depends(rate_limit)) -> dict:
        """The tele lenses this relay can steer, each with the wide lens it pairs with."""
        require_pairing(pairing_code)
        try:
            available = await _load_onvif_blocks()
        except HTTPException:
            available = {}
        return {"cameras": [{"camera": n, "wide": c["wide"], "aim": True}
                            for n, c in CALIBRATION.items() if n in available]}

    @router.get("/{name}/state")
    async def state(name: str, pairing_code: str = "", presets: bool = False,
                    _: None = Depends(move_rate_limit)) -> dict:
        require_pairing(pairing_code)
        cam = await camera(name)
        try:
            pan, tilt = await cam.status()
            out = {"camera": name, "wide": CALIBRATION[name]["wide"], "pan": pan, "tilt": tilt,
                   "aim": aim_box(CALIBRATION[name], pan, tilt)}
            if presets:
                out["presets"] = await cam.presets()
        except OnvifError as exc:
            raise _bad_gateway(exc)
        return out

    @router.post("/{name}/move")
    async def move(name: str, body: MoveIn, _: None = Depends(move_rate_limit)) -> dict:
        require_pairing(body.pairing_code)
        cam = await camera(name)
        vx, vy = _clamp(body.x, -1.0, 1.0), _clamp(body.y, -1.0, 1.0)
        try:
            if abs(vx) < 0.02 and abs(vy) < 0.02:
                _disarm_lease(name)
                await cam.stop()
                return {"ok": True, "moving": False}
            await cam.continuous(vx, vy)
        except OnvifError as exc:
            _disarm_lease(name)
            raise _bad_gateway(exc)
        _arm_lease(name, cam)
        return {"ok": True, "moving": True, "lease": MOVE_LEASE_SECONDS}

    @router.post("/{name}/aim")
    async def aim(name: str, body: AimIn, _: None = Depends(rate_limit)) -> dict:
        require_pairing(body.pairing_code)
        cam = await camera(name)
        cal = CALIBRATION[name]
        pan, tilt = solve(cal, _clamp(body.x, 0.0, 1.0), _clamp(body.y, 0.0, 1.0))
        _disarm_lease(name)
        try:
            await cam.absolute(pan, tilt)
        except OnvifError as exc:
            raise _bad_gateway(exc)
        return {"ok": True, "pan": pan, "tilt": tilt, "aim": aim_box(cal, pan, tilt)}

    @router.post("/{name}/presets")
    async def add_preset(name: str, body: PresetIn, _: None = Depends(rate_limit)) -> dict:
        require_pairing(body.pairing_code)
        label = clean_preset_name(body.name)
        if not label:
            raise HTTPException(status_code=400, detail="give the spot a name")
        cam = await camera(name)
        try:
            token = await cam.set_preset(label)
            return {"ok": True, "token": token, "name": label, "presets": await cam.presets()}
        except OnvifError as exc:
            raise _bad_gateway(exc)

    @router.post("/{name}/presets/{token}/goto")
    async def goto_preset(name: str, token: str, body: CodeIn, _: None = Depends(rate_limit)) -> dict:
        require_pairing(body.pairing_code)
        cam = await camera(name)
        _disarm_lease(name)
        try:
            await cam.goto_preset(token)
        except OnvifError as exc:
            raise _bad_gateway(exc)
        return {"ok": True}

    @router.post("/{name}/presets/{token}/delete")
    async def delete_preset(name: str, token: str, body: CodeIn, _: None = Depends(rate_limit)) -> dict:
        require_pairing(body.pairing_code)
        cam = await camera(name)
        try:
            await cam.remove_preset(token)
            return {"ok": True, "presets": await cam.presets()}
        except OnvifError as exc:
            raise _bad_gateway(exc)

    return router
