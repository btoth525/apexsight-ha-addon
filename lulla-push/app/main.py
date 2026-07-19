"""Lulla Push + Sync relay — FastAPI app.

Self-hosted household sync (plan §2.3 Path D, docs/DECISIONS.md D-003) + the APNs push
relay (§7, mirrors apexsight-push). This file wires the **sync** surface; push endpoints
land in Phase 6.5 alongside the shared APNs .p8.

Public API (called by the iOS app):
  POST /v1/register     — a phone joins a household with the pairing code → bearer token
  POST /v1/sync/push    — push locally-changed records (dedupe + LWW on the server)
  GET  /v1/sync/pull    — pull everything since the device's cursor
  GET  /v1/sync/state   — counts (also feeds the admin dashboard)
  GET  /healthz         — liveness
"""
import time
from datetime import datetime
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import apns, config, db, home, routing, security

app = FastAPI(title="Lulla Push + Sync Relay", docs_url=None, redoc_url=None)

# Public-internet auth hardening (this relay is reachable via the Cloudflare Tunnel):
# slow brute force against the pairing code well below its 36^8 keyspace, and never leak
# it via timing. Exempt only the test harness's ACCEPT_ANY_PAIRING mode, which registers
# many households per run and is never reachable outside `swift test`.
_register_limiter = security.RateLimiter(max_attempts=10, window_seconds=300)
_admin_limiter = security.RateLimiter(max_attempts=5, window_seconds=300)


def _client_key(request: Request) -> str:
    # Honor Cloudflare's real-client-IP header when present (the tunnel proxies from it),
    # else fall back to the socket peer.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


@app.on_event("startup")
async def _startup() -> None:
    db.init()


class APNsConfigBody(BaseModel):
    pairing_code: str
    p8: str
    key_id: str
    team_id: str
    bundle_id: str
    env_mode: str = "auto"


@app.post("/v1/admin/apns")
async def set_apns_config(body: APNsConfigBody, request: Request):
    """One-shot APNs credential load (stand-in for the admin GUI). Pairing-code protected;
    the .p8 lands only in /data (db.config), never in the repo."""
    if not _admin_limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="too many attempts, try again later")
    if not security.safe_equals(body.pairing_code.upper().strip(), config.PAIRING_CODE):
        raise HTTPException(status_code=403, detail="pairing code mismatch")
    db.set_config("apns_p8", body.p8)
    db.set_config("apns_key_id", body.key_id)
    db.set_config("apns_team_id", body.team_id)
    db.set_config("apns_bundle_id", body.bundle_id)
    db.set_config("apns_env_mode", body.env_mode)
    return {"ok": True, "apns_configured": True}


# ---- models -----------------------------------------------------------------

class RegisterBody(BaseModel):
    pairing_code: str
    device_id: str
    device_name: Optional[str] = None
    # Optional push registration (§7.2): when device_token is present, the phone is also
    # recorded in the push registry so it can receive APNs. env is tracked PER token.
    parent_id: Optional[str] = None
    device_token: Optional[str] = None
    push_env: Optional[str] = None            # "prod" | "sandbox"
    push_to_start_token: Optional[str] = None
    app_version: Optional[str] = None


class SyncRecord(BaseModel):
    type: str
    id: str
    updated_at: float          # epoch seconds (Swift Date → timeIntervalSince1970)
    created_by: str = ""
    is_tombstoned: bool = False
    payload: str               # opaque JSON string (the LogEventSnapshot etc.)


class PushBody(BaseModel):
    records: list[SyncRecord] = Field(default_factory=list)


# ---- auth -------------------------------------------------------------------

def _household(authorization: Optional[str] = Header(default=None)) -> str:
    """Resolve the bearer token to a household. Every sync call is scoped to it."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    row = db.resolve_token(token)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return row["household"]


# ---- endpoints --------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    st = db.global_stats()
    return {
        "status": "ok",
        "service": "lulla-push",
        "pairing_code_set": bool(config.PAIRING_CODE),
        "apns_configured": apns.get_client().is_configured(),
        "records": st["records"],
        "devices": st["devices"],
        "push_devices": len(db.push_devices()),
        "households": st["households"],
    }


@app.post("/v1/register")
async def register(body: RegisterBody, request: Request):
    code = body.pairing_code.upper().strip()
    if config.ACCEPT_ANY_PAIRING:
        household = code                       # TEST mode: pairing code IS the household
    else:
        if not _register_limiter.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="too many attempts, try again later")
        if not security.safe_equals(code, config.PAIRING_CODE):
            raise HTTPException(status_code=403, detail="pairing code mismatch")
        household = config.PAIRING_CODE
    token = db.register_device(household, body.device_id, body.device_name)
    if body.device_token:
        db.upsert_push_device(
            device_token=body.device_token,
            household=household,
            parent_id=body.parent_id or body.device_id,
            env=(body.push_env or "prod"),
            push_to_start_token=body.push_to_start_token,
            app_version=body.app_version,
        )
    return {"token": token, "household": household}


@app.post("/v1/sync/push")
async def sync_push(body: PushBody, household: str = Depends(_household)):
    applied = 0
    max_seq = 0
    for r in body.records:
        res = db.upsert(household, r.type, r.id, r.updated_at, r.created_by,
                        r.is_tombstoned, r.payload)
        if res["applied"]:
            applied += 1
        max_seq = max(max_seq, res["server_seq"])
    return {"applied": applied, "received": len(body.records), "cursor": max_seq}


@app.get("/v1/sync/pull")
async def sync_pull(since: int = 0, limit: int = 500, household: str = Depends(_household)):
    return db.pull(household, since, limit)


@app.get("/v1/sync/state")
async def sync_state(household: str = Depends(_household)):
    return db.state(household)


# ---- the house (Phase 5, §6) — read straight from HA, zero app-side setup ---

class ToggleBody(BaseModel):
    entity_id: str


@app.get("/v1/home/state")
async def home_state(household: str = Depends(_household)):
    """Owlet vitals + the nursery strip, auto-discovered by entity name. `connected` tells
    the app whether this add-on could reach Home Assistant's own API at all — independent
    of whether any matching entities exist yet."""
    return await home.state()


@app.post("/v1/home/toggle")
async def home_toggle(body: ToggleBody, household: str = Depends(_household)):
    ok = await home.toggle(body.entity_id)
    return {"ok": ok}


# ---- push / eventing (Phase 6.5, plan §7) -----------------------------------

class ActivityRegisterBody(BaseModel):
    activity_id: str
    push_token: str
    kind: str = ""
    child_id: str = ""
    env: str = "prod"                      # per-token APNs env (sandbox/prod), §7.3


class PushEventBody(BaseModel):
    event: str
    child_id: str = ""
    title: str = ""
    body: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    exclude_parent_id: Optional[str] = None
    interruption_level: str = "active"     # active | passive | time-sensitive | critical
    collapse_id: str = ""
    category: str = ""
    household: Optional[str] = None
    child_asleep: bool = False             # caller (HA/app) reports the child's sleep state


class ActivityStartBody(BaseModel):
    child_id: str = ""
    kind: str = ""
    attributes_type: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    content_state: dict[str, Any] = Field(default_factory=dict)
    stale_date: Optional[int] = None
    exclude_parent_id: Optional[str] = None
    household: Optional[str] = None


class ActivityUpdateBody(BaseModel):
    activity_id: str = ""
    child_id: str = ""
    content_state: dict[str, Any] = Field(default_factory=dict)
    stale_date: Optional[int] = None


class ActivityEndBody(BaseModel):
    activity_id: str = ""
    child_id: str = ""
    content_state: dict[str, Any] = Field(default_factory=dict)
    dismissal_date: Optional[int] = None


class TestBody(BaseModel):
    title: str = "Lulla test"
    body: str = "This is a test notification."
    critical: bool = False
    household: Optional[str] = None


class HeartbeatBody(BaseModel):
    parent_id: Optional[str] = None
    device_id: Optional[str] = None
    ha_ok: bool = True                     # app relays whether HA looked reachable
    owlet_unavailable: bool = False


# Watchdog thresholds (seconds). The app should heartbeat well inside these.
HEARTBEAT_TIMEOUT = 15 * 60
HA_TIMEOUT = 15 * 60


def _now_local_minutes() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


async def _send_and_log(client, event, token, env, payload, *, push_type, collapse_id="",
                        topic_override=None):
    """Send one push, log it, and prune the token on 410 / dead-token. Returns detail."""
    ok, status, reason = await client.send_to_token(
        token, env, payload, push_type=push_type, collapse_id=collapse_id,
        topic_override=topic_override,
    )
    db.log_delivery(event, status, reason)
    # Feed the watchdog's push-channel signal (§7.7). A genuine TRANSPORT failure — network
    # error (status 0) or APNs 5xx — means the alert channel itself is down; a dead-token
    # 410 is routine housekeeping and must NOT be read as a chain break. A success clears it.
    if ok:
        db.set_monitoring("last_push_error", 0.0)
    elif status == 0 or status >= 500:
        db.set_monitoring("last_push_error", time.time())
    if not ok and apns.is_dead_token(status, reason):
        db.delete_push_device(token)
        return {"token": token, "ok": False, "status": status, "reason": reason, "pruned": True}
    return {"token": token, "ok": ok, "status": status, "reason": reason, "pruned": False}


@app.post("/v1/register/activity")
async def register_activity(body: ActivityRegisterBody):
    db.register_activity(body.activity_id, body.child_id, body.kind, body.push_token, body.env)
    return {"status": "ok", "activity_id": body.activity_id}


@app.post("/v1/push")
async def push(body: PushEventBody):
    """Fan an event out to push devices, applying §7.4 routing (non-negotiable):
    never notify exclude_parent_id, collapse by collapse_id, respect quiet hours except
    time-sensitive, and downgrade non-urgent to silent when nap_aware + child asleep."""
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")

    in_quiet = routing.is_quiet_hours(
        _now_local_minutes(),
        routing.hhmm_to_minutes(config.QUIET_HOURS_START),
        routing.hhmm_to_minutes(config.QUIET_HOURS_END),
    )
    decision = routing.route_event(
        interruption_level=body.interruption_level,
        in_quiet_hours=in_quiet,
        nap_aware=config.NAP_AWARE,
        child_asleep=body.child_asleep,
    )
    if not decision.deliver:
        db.log_delivery(body.event, 0, decision.reason)
        return {"event": body.event, "delivered": 0, "suppressed": True,
                "reason": decision.reason}

    if decision.silent:
        payload = apns.build_background_payload(data={"event": body.event, **body.data})
        push_type = "background"
    elif body.interruption_level == "critical":
        payload = apns.build_critical_payload(
            title=body.title, body=body.body, category=body.category,
            data={"event": body.event, **body.data},
        )
        push_type = "alert"
    else:
        payload = apns.build_alert_payload(
            title=body.title, body=body.body, category=body.category,
            interruption_level=body.interruption_level,
            data={"event": body.event, **body.data},
        )
        push_type = "alert"

    results = []
    for dev in db.push_devices(body.household):
        if body.exclude_parent_id and dev["parent_id"] == body.exclude_parent_id:
            continue
        results.append(await _send_and_log(
            client, body.event, dev["device_token"], dev["env"], payload,
            push_type=push_type, collapse_id=body.collapse_id,
        ))
    delivered = sum(1 for r in results if r["ok"])
    pruned = sum(1 for r in results if r["pruned"])
    return {"event": body.event, "delivered": delivered, "suppressed": False,
            "silent": decision.silent, "pruned": pruned, "results": results}


@app.post("/v1/activity/start")
async def activity_start(body: ActivityStartBody):
    """Push-to-start a Live Activity on the OTHER parent's phone (iOS 17.2+)."""
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")
    payload = apns.build_liveactivity_payload(
        event="start", content_state=body.content_state,
        stale_date=body.stale_date, attributes_type=body.attributes_type,
        attributes=body.attributes,
    )
    results = []
    for dev in db.push_devices(body.household):
        if body.exclude_parent_id and dev["parent_id"] == body.exclude_parent_id:
            continue
        if not dev["push_to_start_token"]:
            continue
        results.append(await _send_and_log(
            client, "activity.start", dev["push_to_start_token"], dev["env"], payload,
            push_type="liveactivity",
        ))
    delivered = sum(1 for r in results if r["ok"])
    return {"event": "activity.start", "delivered": delivered, "results": results}


@app.post("/v1/activity/update")
async def activity_update(body: ActivityUpdateBody):
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")
    acts = db.activities_for(activity_id=body.activity_id, child_id=body.child_id)
    if not acts:
        raise HTTPException(status_code=404, detail="no matching activity")
    payload = apns.build_liveactivity_payload(
        event="update", content_state=body.content_state, stale_date=body.stale_date,
    )
    results = []
    for act in acts:
        results.append(await _send_and_log(
            client, "activity.update", act["push_token"], act["env"], payload,
            push_type="liveactivity",
        ))
    delivered = sum(1 for r in results if r["ok"])
    return {"event": "activity.update", "delivered": delivered, "results": results}


@app.post("/v1/activity/end")
async def activity_end(body: ActivityEndBody):
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")
    acts = db.activities_for(activity_id=body.activity_id, child_id=body.child_id)
    if not acts:
        raise HTTPException(status_code=404, detail="no matching activity")
    payload = apns.build_liveactivity_payload(
        event="end", content_state=body.content_state, dismissal_date=body.dismissal_date,
    )
    results = []
    for act in acts:
        results.append(await _send_and_log(
            client, "activity.end", act["push_token"], act["env"], payload,
            push_type="liveactivity",
        ))
        db.delete_activity(act["activity_id"])
    delivered = sum(1 for r in results if r["ok"])
    return {"event": "activity.end", "delivered": delivered, "results": results}


@app.post("/v1/test")
async def test_push(body: TestBody):
    """GUI 'send test notification' — also the 'Test critical alert' button (§7.7)."""
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")
    if body.critical:
        payload = apns.build_critical_payload(title=body.title, body=body.body)
    else:
        payload = apns.build_alert_payload(title=body.title, body=body.body)
    results = []
    for dev in db.push_devices(body.household):
        results.append(await _send_and_log(
            client, "test", dev["device_token"], dev["env"], payload, push_type="alert",
        ))
    delivered = sum(1 for r in results if r["ok"])
    return {"event": "test", "delivered": delivered, "results": results}


# ---- supervised watchdog (§7.7) ---------------------------------------------

@app.post("/v1/heartbeat")
async def heartbeat(body: HeartbeatBody):
    """The app checks in. Records last-heartbeat + the HA/Owlet health it observed so the
    watchdog can tell 'watching and fine' from 'quietly broken'."""
    now = time.time()
    db.set_monitoring("last_heartbeat", now)
    if body.ha_ok:
        db.set_monitoring("ha_last_seen", now)
    db.set_monitoring("owlet_unavailable", 1.0 if body.owlet_unavailable else 0.0)
    return {"status": "ok", "ts": now}


def _watchdog_decision(now: Optional[float] = None) -> routing.WatchdogDecision:
    now = now if now is not None else time.time()
    # last_push_error is set only on a real transport failure and cleared on the next
    # success, so it reflects the CURRENT health of the push channel (not a stale 410).
    undeliverable = bool(db.get_monitoring("last_push_error"))
    return routing.evaluate_watchdog(
        now=now,
        last_heartbeat=db.get_monitoring("last_heartbeat"),
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        ha_last_seen=db.get_monitoring("ha_last_seen"),
        ha_timeout=HA_TIMEOUT,
        owlet_unavailable=bool(db.get_monitoring("owlet_unavailable")),
        undeliverable=undeliverable,
    )


@app.post("/v1/watchdog/run")
async def watchdog_run():
    """Evaluate the monitoring chain; fire monitoring.chain_broken as a CRITICAL alert if
    any link is stale. Meant to be poked on a schedule (HA automation / cron)."""
    decision = _watchdog_decision()
    if not decision.fire:
        return {"fired": False, "status": decision.status, "reason": decision.reason}
    client = apns.get_client()
    fired_to = 0
    if client.is_configured():
        payload = apns.build_critical_payload(
            title="Monitoring stopped",
            body=f"Lulla can't confirm the baby is being watched: {decision.reason}.",
            data={"event": "monitoring.chain_broken", "reason": decision.reason},
        )
        for dev in db.push_devices():
            res = await _send_and_log(
                client, "monitoring.chain_broken", dev["device_token"], dev["env"],
                payload, push_type="alert", collapse_id="lulla-chain-broken",
            )
            if res["ok"]:
                fired_to += 1
    else:
        db.log_delivery("monitoring.chain_broken", 0, "APNs not configured")
    return {"fired": True, "status": decision.status, "reason": decision.reason,
            "delivered": fired_to}


@app.get("/v1/monitoring/status")
async def monitoring_status():
    """Status pip accessor for the app's Today screen (green/amber/red + last-checked)."""
    decision = _watchdog_decision()
    last_hb = db.get_monitoring("last_heartbeat")
    return {
        "status": decision.status,
        "reason": decision.reason,
        "healthy": not decision.fire,
        "last_heartbeat": last_hb,
        "checked_at": time.time(),
    }
