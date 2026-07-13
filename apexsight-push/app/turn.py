"""Cloudflare Realtime TURN credential minting.

Frigate 0.18 removed the go2rtc HLS live route, so live view is WebRTC-only. WebRTC media
cannot ride the household's Cloudflare HTTP tunnel — on the LAN it connects straight to go2rtc's
host candidate, but away from home that candidate is unreachable. The fix every commercial camera
app uses is a TURN relay: the phone gathers a relay candidate on a public host, and go2rtc sends
media to it. We mint short-lived Cloudflare Realtime TURN credentials so the phone (live view AND
two-way talk) can relay through Cloudflare's edge when direct fails.

Security: the relayed media is end-to-end DTLS-encrypted — Cloudflare only forwards packets, it
cannot see the camera. Minting is gated by the household pairing code in main.py, and the
Cloudflare API token never leaves the relay.

The generated credentials are valid for `_TTL` and are HMAC-derived, so the SAME set is safely
shared by every phone in the household — we cache one set and re-mint only near expiry, so a wall
of phones opening streams doesn't hammer the Cloudflare API.
"""
import os
import time

import httpx

from . import db

# Cloudflare mints creds valid for this long; we hand out the same set until it's close to expiry.
_TTL = 86400            # 24h
_REFRESH_MARGIN = 3600  # re-mint when < 1h remains, so a handed-out cred is always comfortably valid

_GENERATE_URL = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"

_cached: list | None = None
_cached_exp: float = 0.0


class TurnNotConfigured(Exception):
    pass


def _key() -> tuple[str, str]:
    """The Cloudflare TURN key (Key ID + API token). Env (add-on config) first, then the DB config
    table (in case it's ever set through the admin GUI). Secrets never come from the repo."""
    key_id = (os.environ.get("TURN_KEY_ID") or db.get_config("turn_key_id") or "").strip()
    api_token = (os.environ.get("TURN_API_TOKEN") or db.get_config("turn_api_token") or "").strip()
    return key_id, api_token


def is_configured() -> bool:
    key_id, api_token = _key()
    return bool(key_id and api_token)


async def ice_servers() -> list:
    """Return a list of ICE server dicts ({urls, username?, credential?}) — the exact shape the iOS
    app decodes. Cached until near expiry. Raises TurnNotConfigured if no key is set, or the
    underlying httpx error if Cloudflare rejects the request (surfaced as a 502 by the caller)."""
    global _cached, _cached_exp
    now = time.time()
    if _cached is not None and now < _cached_exp - _REFRESH_MARGIN:
        return _cached

    key_id, api_token = _key()
    if not (key_id and api_token):
        raise TurnNotConfigured("TURN key not configured on relay")

    url = _GENERATE_URL.format(key_id=key_id)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            json={"ttl": _TTL},
        )
    resp.raise_for_status()
    servers = resp.json().get("iceServers", [])
    _cached = servers
    _cached_exp = now + _TTL
    return servers
