"""APNs token-based (.p8) push sender.

Uses HTTP/2 to Apple's APNs with a provider JWT signed by your .p8 key
(ES256). The same provider token works for both the production and sandbox
endpoints; we pick the host per-device from the environment recorded at
registration.

No secrets live here — the .p8 PEM, Key ID, Team ID and Bundle ID are read
from the DB config table (populated via the admin GUI).
"""
import json
import time
from typing import Optional

import httpx
import jwt

from . import db

PROD_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"

# Apple requires the provider token be refreshed no more than once every 20
# minutes and treated as stale after 60. Refresh at 45 to stay safely inside.
_TOKEN_TTL = 45 * 60

_cached_token: Optional[str] = None
_cached_at: float = 0.0
_cached_kid: Optional[str] = None


class APNsNotConfigured(Exception):
    pass


def _credentials() -> tuple[str, str, str, str, str]:
    p8 = db.get_config("apns_p8")
    key_id = db.get_config("apns_key_id")
    team_id = db.get_config("apns_team_id")
    bundle_id = db.get_config("apns_bundle_id")
    env_mode = db.get_config("apns_env_mode", "auto")
    if not (p8 and key_id and team_id and bundle_id):
        raise APNsNotConfigured(
            "APNs is not fully configured. Upload your .p8 and set Key ID, "
            "Team ID and Bundle ID in the admin settings page."
        )
    return p8, key_id, team_id, bundle_id, env_mode


def _provider_token(p8: str, key_id: str, team_id: str) -> str:
    global _cached_token, _cached_at, _cached_kid
    now = time.time()
    if _cached_token and _cached_kid == key_id and (now - _cached_at) < _TOKEN_TTL:
        return _cached_token
    token = jwt.encode(
        {"iss": team_id, "iat": int(now)},
        p8,
        algorithm="ES256",
        headers={"kid": key_id},
    )
    _cached_token, _cached_at, _cached_kid = token, now, key_id
    return token


def _host_for(environment: str, env_mode: str) -> str:
    # env_mode "auto" trusts what the device reported at registration;
    # forcing "production"/"sandbox" overrides every device (useful for debugging).
    effective = environment if env_mode == "auto" else env_mode
    return SANDBOX_HOST if effective == "sandbox" else PROD_HOST


def is_configured() -> bool:
    try:
        _credentials()
        return True
    except APNsNotConfigured:
        return False


async def send_to_token(
    device_token: str, environment: str, payload: dict, collapse_id: str = ""
) -> tuple[bool, str]:
    """Send one push. Returns (ok, detail). detail is APNs' reason on failure."""
    p8, key_id, team_id, bundle_id, env_mode = _credentials()
    headers = {
        "authorization": f"bearer {_provider_token(p8, key_id, team_id)}",
        "apns-topic": bundle_id,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    if collapse_id:
        # APNs caps the collapse identifier at 64 bytes. Reusing it across the
        # instant alert and the follow-up full-GIF push makes the second one
        # replace the first in place instead of stacking a duplicate.
        headers["apns-collapse-id"] = collapse_id[:64]
    url = f"{_host_for(environment, env_mode)}/3/device/{device_token}"
    try:
        async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
            resp = await client.post(url, headers=headers, content=json.dumps(payload))
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"

    if resp.status_code == 200:
        return True, "ok"
    reason = ""
    try:
        reason = resp.json().get("reason", "")
    except Exception:
        reason = resp.text.strip()
    return False, f"{resp.status_code} {reason}".strip()


def build_payload(
    *,
    title: str,
    body: str,
    camera: str = "",
    review_id: str = "",
    apex_url: str = "",
    snapshot_url: str = "",
    thumbnail_url: str = "",
    snapshot_path: str = "",
    frigate_token: str = "",
    silent: bool = False,
    announce: bool = False,
) -> dict:
    """Build the APNs payload the ApexSight NotificationService extension expects.

    `mutable-content: 1` lets the extension run and attach the snapshot/GIF, so
    the alert renders rich media even when the app is fully closed.

    `silent=True` is used for the follow-up full-event GIF update: it keeps the
    alert visible (so the extension still runs and swaps in the complete GIF via
    the shared collapse-id) but drops the sound and uses a passive interruption
    level so the user isn't buzzed a second time.
    """
    aps = {
        "alert": {"title": title, "body": body},
        "mutable-content": 1,
        "category": "APEX_FRIGATE_ALERT",
    }
    if silent:
        aps["interruption-level"] = "passive"
    elif announce:
        # Announce-able (read aloud by iOS "Announce Notifications" in CarPlay/AirPods) but no
        # second buzz — used for the AI-description follow-up.
        aps["interruption-level"] = "time-sensitive"
    else:
        aps["sound"] = "default"
    if camera:
        aps["thread-id"] = f"apex-{camera}"

    payload: dict = {"aps": aps}
    # Mirror the local-notification userInfo contract exactly.
    if review_id:
        payload["review_id"] = review_id
    if camera:
        payload["camera"] = camera
    if apex_url:
        payload["apex_url"] = apex_url
    elif review_id:
        payload["apex_url"] = f"apex://review?id={review_id}"
    if snapshot_url:
        payload["snapshot_url"] = snapshot_url
    if snapshot_path:
        # Relative path; the extension resolves it against the app-group base URL.
        payload["snapshot_path"] = snapshot_path
    if thumbnail_url:
        payload["thumbnail_url"] = thumbnail_url
    if frigate_token:
        payload["frigate_token"] = frigate_token
    return payload


async def deliver_to_pairing(pairing_code: str, payload: dict, collapse_id: str = "") -> dict:
    """Fan a payload out to every device registered under a pairing code.

    Prunes tokens APNs reports as permanently gone (410 / BadDeviceToken /
    Unregistered) so the table stays clean.
    """
    rows = db.devices_for(pairing_code)
    sent, failed, pruned = 0, 0, 0
    errors: list[str] = []
    for row in rows:
        ok, detail = await send_to_token(row["device_token"], row["environment"], payload, collapse_id)
        if ok:
            sent += 1
            continue
        failed += 1
        errors.append(detail)
        if any(k in detail for k in ("410", "BadDeviceToken", "Unregistered")):
            db.delete_device(row["device_token"])
            pruned += 1
    return {"devices": len(rows), "sent": sent, "failed": failed, "pruned": pruned, "errors": errors}
