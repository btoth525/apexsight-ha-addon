"""APNs token-based (.p8) push client for Lulla.

Mirrors apexsight-push's proven ES256/HTTP-2 sender, adapted for Lulla's richer
payload set (alert, Live Activity update/start/end, background, critical).

Design for testability:
  - The provider JWT + payload builders + header builder are pure functions.
  - The actual network send sits behind a small `Sender` interface, so tests inject
    a fake sender and never touch Apple. `APNsClient` holds an INSTANCE-level JWT
    cache (not module globals) so per-test clients never contaminate each other.
  - Credentials (.p8 PEM / Key ID / Team ID / Bundle ID) come from the /data SQLite
    config table at runtime — NEVER hardcoded, never in the repo.

APNs env (sandbox vs prod) is tracked PER device token (§7.3): TestFlight/App Store =
prod, Xcode-installed = sandbox. The host is chosen per send from the token's recorded
env — mixing them is the #1 "why won't it deliver" bug.
"""
import json
import time
from typing import Callable, Optional, Protocol

import httpx
import jwt

from . import db

PROD_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"

# Apple: refresh the provider token no more than once per 20 min, treat it stale after
# 60. ~50 min keeps us safely inside the window while minimizing signing churn.
TOKEN_TTL = 50 * 60


class APNsNotConfigured(Exception):
    pass


# ---- credentials ------------------------------------------------------------

def db_credentials() -> tuple[str, str, str, str, str]:
    """Read APNs creds from the /data SQLite config table (populated via admin GUI)."""
    p8 = db.get_config("apns_p8")
    key_id = db.get_config("apns_key_id")
    team_id = db.get_config("apns_team_id")
    bundle_id = db.get_config("apns_bundle_id")
    env_mode = db.get_config("apns_env_mode", "auto")
    if not (p8 and key_id and team_id and bundle_id):
        raise APNsNotConfigured(
            "APNs is not fully configured. Upload your .p8 and set Key ID, Team ID "
            "and Bundle ID on the admin settings page."
        )
    return p8, key_id, team_id, bundle_id, env_mode


# ---- provider JWT (pure) ----------------------------------------------------

def build_provider_jwt(p8_pem: str, key_id: str, team_id: str, *, now: Optional[float] = None) -> str:
    """ES256 provider JWT: header {alg:ES256, kid}, claims {iss:TeamID, iat}."""
    iat = int(now if now is not None else time.time())
    return jwt.encode(
        {"iss": team_id, "iat": iat},
        p8_pem,
        algorithm="ES256",
        headers={"kid": key_id},
    )


def host_for(environment: str, env_mode: str = "auto") -> str:
    """Pick the APNs host for a device. `env_mode` "auto" trusts the token's recorded
    env; forcing prod/sandbox overrides every device (debugging)."""
    effective = environment if env_mode == "auto" else env_mode
    return SANDBOX_HOST if effective == "sandbox" else PROD_HOST


# ---- headers (pure) ---------------------------------------------------------

def build_headers(
    *,
    push_type: str,
    bundle_id: str,
    provider_jwt: str,
    priority: Optional[int] = None,
    collapse_id: str = "",
    expiration: Optional[int] = None,
    topic_override: Optional[str] = None,
) -> dict:
    """APNs request headers. Live Activity pushes use the
    `<bundleID>.push-type.liveactivity` topic; everything else uses the bare bundle ID.
    `background` defaults to priority 5 (required); everything else to 10."""
    if topic_override:
        topic = topic_override
    elif push_type == "liveactivity":
        topic = f"{bundle_id}.push-type.liveactivity"
    else:
        topic = bundle_id
    if priority is None:
        priority = 5 if push_type == "background" else 10
    headers = {
        "authorization": f"bearer {provider_jwt}",
        "apns-topic": topic,
        "apns-push-type": push_type,
        "apns-priority": str(priority),
    }
    if collapse_id:
        headers["apns-collapse-id"] = collapse_id[:64]  # APNs caps at 64 bytes
    if expiration is not None:
        headers["apns-expiration"] = str(expiration)
    return headers


# ---- payload builders (pure) ------------------------------------------------

def build_alert_payload(
    *,
    title: str,
    body: str,
    category: str = "",
    interruption_level: str = "active",
    thread_id: str = "",
    data: Optional[dict] = None,
    mutable: bool = True,
) -> dict:
    aps: dict = {"alert": {"title": title, "body": body}, "sound": "default"}
    if interruption_level:
        aps["interruption-level"] = interruption_level
    if category:
        aps["category"] = category
    if thread_id:
        aps["thread-id"] = thread_id
    if mutable:
        aps["mutable-content"] = 1
    payload: dict = {"aps": aps}
    if data:
        payload.update(data)
    return payload


def build_critical_payload(
    *,
    title: str,
    body: str,
    sound_name: str = "nursery-alert.caf",
    volume: float = 1.0,
    category: str = "",
    data: Optional[dict] = None,
) -> dict:
    """Critical alert (§7.7): pierces silent mode / DND / Focus at a volume we set.
    `sound` is an OBJECT with `critical:1`, not a string."""
    aps: dict = {
        "alert": {"title": title, "body": body},
        "interruption-level": "critical",
        "sound": {"critical": 1, "name": sound_name, "volume": volume},
    }
    if category:
        aps["category"] = category
    payload: dict = {"aps": aps}
    if data:
        payload.update(data)
    return payload


def build_liveactivity_payload(
    *,
    event: str,  # "start" | "update" | "end"
    content_state: dict,
    timestamp: Optional[int] = None,
    stale_date: Optional[int] = None,
    dismissal_date: Optional[int] = None,
    attributes_type: str = "",
    attributes: Optional[dict] = None,
    alert: Optional[dict] = None,
) -> dict:
    """Live Activity push. `content-state` must match the app's
    ActivityAttributes.ContentState Codable shape exactly. `start` (push-to-start)
    additionally carries `attributes-type` + `attributes`; `update`/`end` omit them."""
    aps: dict = {
        "timestamp": int(timestamp if timestamp is not None else time.time()),
        "event": event,
        "content-state": content_state,
    }
    if stale_date is not None:
        aps["stale-date"] = stale_date
    if dismissal_date is not None:
        aps["dismissal-date"] = dismissal_date
    if event == "start":
        if attributes_type:
            aps["attributes-type"] = attributes_type
        if attributes is not None:
            aps["attributes"] = attributes
    if alert is not None:
        aps["alert"] = alert
    return {"aps": aps}


def build_background_payload(*, data: Optional[dict] = None) -> dict:
    """Silent background push (widget/timeline nudge): content-available, no alert."""
    payload: dict = {"aps": {"content-available": 1}}
    if data:
        payload.update(data)
    return payload


# ---- sender interface -------------------------------------------------------

class Sender(Protocol):
    async def send(self, url: str, headers: dict, body: str) -> tuple[int, str]:
        """Return (status_code, reason_text). reason_text is APNs' `reason` on failure."""
        ...


class HTTP2Sender:
    """Real network sender: HTTP/2 POST to Apple. Wrapped so tests never need it."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def send(self, url: str, headers: dict, body: str) -> tuple[int, str]:
        try:
            async with httpx.AsyncClient(http2=True, timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, content=body)
        except httpx.HTTPError as exc:
            return 0, f"network error: {exc}"
        if resp.status_code == 200:
            return 200, "ok"
        try:
            reason = resp.json().get("reason", "")
        except Exception:
            reason = resp.text.strip()
        return resp.status_code, reason


# APNs statuses that mean the token is permanently dead and should be pruned.
_DEAD_REASONS = ("BadDeviceToken", "Unregistered")


def is_dead_token(status_code: int, reason: str) -> bool:
    return status_code == 410 or any(r in (reason or "") for r in _DEAD_REASONS)


# ---- client -----------------------------------------------------------------

class APNsClient:
    """Holds credentials + a sender + an INSTANCE-level JWT cache."""

    def __init__(
        self,
        credentials_provider: Callable[[], tuple[str, str, str, str, str]] = db_credentials,
        sender: Optional[Sender] = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._creds = credentials_provider
        self._sender: Sender = sender or HTTP2Sender()
        self._now = now_fn
        self._jwt: Optional[str] = None
        self._jwt_at: float = 0.0
        self._jwt_kid: Optional[str] = None

    def is_configured(self) -> bool:
        try:
            self._creds()
            return True
        except APNsNotConfigured:
            return False

    def provider_jwt(self) -> str:
        p8, key_id, team_id, _bundle, _mode = self._creds()
        now = self._now()
        if self._jwt and self._jwt_kid == key_id and (now - self._jwt_at) < TOKEN_TTL:
            return self._jwt
        self._jwt = build_provider_jwt(p8, key_id, team_id, now=now)
        self._jwt_at, self._jwt_kid = now, key_id
        return self._jwt

    async def send_to_token(
        self,
        device_token: str,
        environment: str,
        payload: dict,
        *,
        push_type: str = "alert",
        collapse_id: str = "",
        priority: Optional[int] = None,
        expiration: Optional[int] = None,
        topic_override: Optional[str] = None,
    ) -> tuple[bool, int, str]:
        """Send one push. Returns (ok, status_code, reason)."""
        _p8, _kid, _team, bundle_id, env_mode = self._creds()
        headers = build_headers(
            push_type=push_type,
            bundle_id=bundle_id,
            provider_jwt=self.provider_jwt(),
            priority=priority,
            collapse_id=collapse_id,
            expiration=expiration,
            topic_override=topic_override,
        )
        url = f"{host_for(environment, env_mode)}/3/device/{device_token}"
        status, reason = await self._sender.send(url, headers, json.dumps(payload))
        return status == 200, status, reason


# ---- module singleton (patchable for tests) ---------------------------------

_CLIENT: Optional[APNsClient] = None


def get_client() -> APNsClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = APNsClient()
    return _CLIENT


def set_client(client: Optional[APNsClient]) -> None:
    """Inject a client (tests) or reset to None to force a rebuild."""
    global _CLIENT
    _CLIENT = client
