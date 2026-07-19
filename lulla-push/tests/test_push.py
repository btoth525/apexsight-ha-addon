"""Push + eventing + critical-alert tests (Phase 6.5, plan §7).

Pure-function tests (JWT, payload shapes, routing, watchdog) need no network. Endpoint
tests use a fake APNs sender (never touches Apple) + a temp SQLite, mirroring
test_sync.py's fixture. Real ES256 signing uses a locally-generated P-256 key.
"""
import importlib
import json
import tempfile
import types

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient


# ---- test key ---------------------------------------------------------------

def _p256_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


TEAM_ID = "TEAM123456"
KEY_ID = "KEY7654321"
BUNDLE_ID = "family.lulla"


# ---- fake sender ------------------------------------------------------------

class FakeSender:
    """Records every request; returns a scripted or default status."""

    def __init__(self):
        self.calls = []            # list of dicts: url, headers, body
        self.next_status = 200
        self.next_reason = "ok"
        self.script = []           # optional per-call (status, reason) queue

    async def send(self, url, headers, body):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        if self.script:
            return self.script.pop(0)
        return self.next_status, self.next_reason


# ---- fixture ----------------------------------------------------------------

@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("LULLA_DATA_DIR", tmp)
    monkeypatch.setenv("PAIRING_CODE", "LULLA-TEST-0001")
    from app import config as cfg
    importlib.reload(cfg)
    from app import db as dbmod
    importlib.reload(dbmod)
    from app import apns as apnsmod
    importlib.reload(apnsmod)
    from app import routing as routingmod
    importlib.reload(routingmod)
    from app import main as mainmod
    importlib.reload(mainmod)
    dbmod.init()

    # Configure APNs creds in the /data config table (as the admin GUI would).
    pem = _p256_pem()
    dbmod.set_config("apns_p8", pem)
    dbmod.set_config("apns_key_id", KEY_ID)
    dbmod.set_config("apns_team_id", TEAM_ID)
    dbmod.set_config("apns_bundle_id", BUNDLE_ID)
    dbmod.set_config("apns_env_mode", "auto")

    fake = FakeSender()
    client = apnsmod.APNsClient(sender=fake)   # default creds provider reads the DB config
    apnsmod.set_client(client)

    # Pin "now" to daytime so endpoint tests aren't flaky against the wall clock's quiet
    # hours; tests that exercise quiet hours override this explicitly.
    monkeypatch.setattr(mainmod, "_now_local_minutes", lambda: 12 * 60)

    ns = types.SimpleNamespace(
        http=TestClient(mainmod.app), db=dbmod, apns=apnsmod, routing=routingmod,
        main=mainmod, config=cfg, fake=fake, pem=pem,
    )
    yield ns
    apnsmod.set_client(None)


def _register_push(env, device_id, parent_id, token, push_env="prod", pts=None):
    r = env.http.post("/v1/register", json={
        "pairing_code": "LULLA-TEST-0001", "device_id": device_id, "parent_id": parent_id,
        "device_token": token, "push_env": push_env, "push_to_start_token": pts,
    })
    assert r.status_code == 200, r.text
    return r.json()


# ---- JWT structure ----------------------------------------------------------

def test_provider_jwt_structure(env):
    tok = env.apns.build_provider_jwt(env.pem, KEY_ID, TEAM_ID, now=1_700_000_000)
    header = jwt.get_unverified_header(tok)
    assert header["alg"] == "ES256"
    assert header["kid"] == KEY_ID
    # Decode claims with the public key to prove it's a valid ES256 signature.
    priv = serialization.load_pem_private_key(env.pem.encode(), password=None)
    claims = jwt.decode(tok, priv.public_key(), algorithms=["ES256"])
    assert claims["iss"] == TEAM_ID
    assert claims["iat"] == 1_700_000_000


def test_provider_jwt_cached_instance_level(env):
    c = env.apns.APNsClient(sender=env.fake, now_fn=lambda: 1000.0)
    a = c.provider_jwt()
    b = c.provider_jwt()
    assert a == b  # cached, not re-signed


# ---- payload shapes ---------------------------------------------------------

def test_alert_payload_shape(env):
    p = env.apns.build_alert_payload(title="Hi", body="there", category="LOG",
                                     interruption_level="time-sensitive", data={"x": 1})
    aps = p["aps"]
    assert aps["alert"] == {"title": "Hi", "body": "there"}
    assert aps["interruption-level"] == "time-sensitive"
    assert aps["category"] == "LOG"
    assert aps["mutable-content"] == 1
    assert p["x"] == 1


def test_critical_payload_shape(env):
    p = env.apns.build_critical_payload(title="Monitoring stopped", body="broken",
                                        sound_name="nursery-alert.caf", volume=1.0)
    aps = p["aps"]
    assert aps["interruption-level"] == "critical"
    assert aps["sound"] == {"critical": 1, "name": "nursery-alert.caf", "volume": 1.0}
    assert aps["sound"]["critical"] == 1  # sound is an OBJECT, not a string


def test_liveactivity_start_and_update_shapes(env):
    start = env.apns.build_liveactivity_payload(
        event="start", content_state={"elapsed": 0}, timestamp=123,
        attributes_type="LullaTimerAttributes", attributes={"childId": "c1"},
        stale_date=999,
    )
    aps = start["aps"]
    assert aps["event"] == "start"
    assert aps["content-state"] == {"elapsed": 0}
    assert aps["attributes-type"] == "LullaTimerAttributes"
    assert aps["attributes"] == {"childId": "c1"}
    assert aps["stale-date"] == 999
    assert aps["timestamp"] == 123

    upd = env.apns.build_liveactivity_payload(event="update", content_state={"elapsed": 5},
                                              timestamp=200)
    # update omits attributes / attributes-type
    assert "attributes" not in upd["aps"]
    assert "attributes-type" not in upd["aps"]
    assert upd["aps"]["event"] == "update"


def test_background_payload_shape(env):
    p = env.apns.build_background_payload(data={"event": "sync.refresh"})
    assert p["aps"] == {"content-available": 1}
    assert p["event"] == "sync.refresh"
    assert "alert" not in p["aps"]


def test_headers_liveactivity_topic_and_background_priority(env):
    h = env.apns.build_headers(push_type="liveactivity", bundle_id=BUNDLE_ID,
                               provider_jwt="jwt")
    assert h["apns-topic"] == f"{BUNDLE_ID}.push-type.liveactivity"
    assert h["apns-push-type"] == "liveactivity"
    assert h["apns-priority"] == "10"

    bg = env.apns.build_headers(push_type="background", bundle_id=BUNDLE_ID,
                                provider_jwt="jwt")
    assert bg["apns-push-type"] == "background"
    assert bg["apns-priority"] == "5"        # required for background
    assert bg["apns-topic"] == BUNDLE_ID


# ---- per-device env selection -----------------------------------------------

def test_env_selects_host_per_token(env):
    _register_push(env, "phoneS", "mom", "tok-sandbox", push_env="sandbox")
    _register_push(env, "phoneP", "dad", "tok-prod", push_env="prod")
    r = env.http.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b"})
    assert r.status_code == 200, r.text
    hosts = {c["url"].split("/3/device/")[1]: c["url"] for c in env.fake.calls}
    assert "sandbox" in hosts["tok-sandbox"]
    assert "sandbox" not in hosts["tok-prod"]
    assert hosts["tok-prod"].startswith("https://api.push.apple.com")


# ---- 410 prunes the token ---------------------------------------------------

def test_410_prunes_token(env):
    _register_push(env, "phoneA", "mom", "dead-tok", push_env="prod")
    env.fake.next_status, env.fake.next_reason = 410, "Unregistered"
    r = env.http.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b"})
    assert r.status_code == 200, r.text
    assert r.json()["pruned"] == 1
    assert env.db.push_devices() == []  # token removed


# ---- routing: exclude the actor ---------------------------------------------

def test_push_excludes_actor(env):
    _register_push(env, "phoneA", "mom", "tok-mom")
    _register_push(env, "phoneB", "dad", "tok-dad")
    r = env.http.post("/v1/push", json={
        "event": "event.logged", "title": "Dad logged a bottle", "body": "4 oz",
        "exclude_parent_id": "dad"})
    assert r.status_code == 200, r.text
    sent_tokens = [c["url"].split("/3/device/")[1] for c in env.fake.calls]
    assert "tok-mom" in sent_tokens
    assert "tok-dad" not in sent_tokens        # never notify the parent who acted


# ---- routing: collapse id ---------------------------------------------------

def test_push_sets_collapse_id(env):
    _register_push(env, "phoneA", "mom", "tok-mom")
    env.http.post("/v1/push", json={
        "event": "feed.overdue", "title": "t", "body": "b", "collapse_id": "feed-c1"})
    assert env.fake.calls[0]["headers"]["apns-collapse-id"] == "feed-c1"


# ---- routing: quiet hours (pure + endpoint) ---------------------------------

def test_is_quiet_hours_wrapping_window(env):
    r = env.routing
    assert r.is_quiet_hours(23 * 60, 22 * 60, 7 * 60) is True     # 23:00 in 22:00-07:00
    assert r.is_quiet_hours(3 * 60, 22 * 60, 7 * 60) is True      # 03:00 wraps
    assert r.is_quiet_hours(12 * 60, 22 * 60, 7 * 60) is False    # noon awake


def test_quiet_hours_suppresses_non_urgent(env, monkeypatch):
    _register_push(env, "phoneA", "mom", "tok-mom")
    monkeypatch.setattr(env.main, "_now_local_minutes", lambda: 3 * 60)  # 03:00
    monkeypatch.setattr(env.config, "QUIET_HOURS_START", "22:00")
    monkeypatch.setattr(env.config, "QUIET_HOURS_END", "07:00")
    # non-urgent → suppressed
    r = env.http.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b"})
    assert r.json()["suppressed"] is True
    assert len(env.fake.calls) == 0
    # time-sensitive pierces quiet hours
    r2 = env.http.post("/v1/push", json={
        "event": "feed.overdue", "title": "t", "body": "b",
        "interruption_level": "time-sensitive"})
    assert r2.json()["suppressed"] is False
    assert len(env.fake.calls) == 1


# ---- routing: nap-aware downgrade -------------------------------------------

def test_nap_aware_downgrades_to_silent(env, monkeypatch):
    _register_push(env, "phoneA", "mom", "tok-mom")
    monkeypatch.setattr(env.main, "_now_local_minutes", lambda: 12 * 60)  # daytime, not quiet
    monkeypatch.setattr(env.config, "NAP_AWARE", True)
    r = env.http.post("/v1/push", json={
        "event": "event.logged", "title": "t", "body": "b", "child_asleep": True})
    body = r.json()
    assert body["suppressed"] is False
    assert body["silent"] is True
    # delivered as a silent BACKGROUND push, not an alert banner
    call = env.fake.calls[0]
    assert call["headers"]["apns-push-type"] == "background"
    assert call["body"]["aps"] == {"content-available": 1}


def test_route_event_urgent_ignores_nap_and_quiet(env):
    r = env.routing
    d = r.route_event(interruption_level="critical", in_quiet_hours=True,
                      nap_aware=True, child_asleep=True)
    assert d.deliver is True and d.silent is False and d.push_type == "alert"


# ---- Live Activity endpoints ------------------------------------------------

def test_activity_start_uses_push_to_start_token(env):
    _register_push(env, "phoneB", "dad", "tok-dad", pts="pts-dad")
    _register_push(env, "phoneNo", "mom", "tok-mom")  # no push-to-start token
    r = env.http.post("/v1/activity/start", json={
        "child_id": "c1", "kind": "sleep", "attributes_type": "LullaTimerAttributes",
        "attributes": {"childId": "c1"}, "content_state": {"elapsed": 0},
        "exclude_parent_id": "mom"})
    assert r.status_code == 200, r.text
    tokens = [c["url"].split("/3/device/")[1] for c in env.fake.calls]
    assert tokens == ["pts-dad"]               # only the device with a PTS token
    assert env.fake.calls[0]["headers"]["apns-push-type"] == "liveactivity"
    assert env.fake.calls[0]["body"]["aps"]["event"] == "start"


def test_activity_update_follows_per_token_env(env):
    # A Live Activity registered from an Xcode (sandbox) build must push to the SANDBOX
    # host — hardcoding prod is the #1 delivery bug (§7.3).
    env.http.post("/v1/register/activity", json={
        "activity_id": "actS", "push_token": "la-sandbox", "kind": "sleep",
        "child_id": "c1", "env": "sandbox"})
    env.http.post("/v1/activity/update", json={
        "activity_id": "actS", "content_state": {"elapsed": 1}})
    assert "sandbox" in env.fake.calls[-1]["url"]


def test_activity_update_and_end(env):
    env.http.post("/v1/register/activity", json={
        "activity_id": "act1", "push_token": "la-tok", "kind": "sleep", "child_id": "c1"})
    upd = env.http.post("/v1/activity/update", json={
        "activity_id": "act1", "content_state": {"elapsed": 60}})
    assert upd.status_code == 200, upd.text
    assert env.fake.calls[-1]["body"]["aps"]["event"] == "update"
    end = env.http.post("/v1/activity/end", json={
        "activity_id": "act1", "content_state": {"elapsed": 90}, "dismissal_date": 5})
    assert end.status_code == 200, end.text
    assert env.fake.calls[-1]["body"]["aps"]["event"] == "end"
    # activity registration is cleaned up on end
    assert env.db.activities_for(activity_id="act1") == []


# ---- watchdog (pure + endpoint) ---------------------------------------------

def test_evaluate_watchdog_fires_when_stale(env):
    d = env.routing.evaluate_watchdog(
        now=10_000, last_heartbeat=None, heartbeat_timeout=900,
        ha_last_seen=None, ha_timeout=900, owlet_unavailable=False, undeliverable=False)
    assert d.fire is True and d.status == "red"
    assert "heartbeat" in d.reason


def test_evaluate_watchdog_quiet_when_healthy(env):
    now = 10_000
    d = env.routing.evaluate_watchdog(
        now=now, last_heartbeat=now - 10, heartbeat_timeout=900,
        ha_last_seen=now - 10, ha_timeout=900, owlet_unavailable=False, undeliverable=False)
    assert d.fire is False and d.status == "green" and d.reason == "healthy"


def test_evaluate_watchdog_fires_on_owlet_unavailable(env):
    now = 10_000
    d = env.routing.evaluate_watchdog(
        now=now, last_heartbeat=now - 10, heartbeat_timeout=900,
        ha_last_seen=now - 10, ha_timeout=900, owlet_unavailable=True, undeliverable=False)
    assert d.fire is True and "Owlet" in d.reason


def test_watchdog_endpoint_fires_critical_when_no_heartbeat(env):
    _register_push(env, "phoneA", "mom", "tok-mom")
    r = env.http.post("/v1/watchdog/run")
    body = r.json()
    assert body["fired"] is True
    assert body["delivered"] == 1
    call = env.fake.calls[-1]
    assert call["body"]["aps"]["interruption-level"] == "critical"
    assert call["body"]["event"] == "monitoring.chain_broken"


def test_watchdog_endpoint_quiet_when_healthy(env):
    _register_push(env, "phoneA", "mom", "tok-mom")
    env.http.post("/v1/heartbeat", json={"parent_id": "mom", "ha_ok": True})
    r = env.http.post("/v1/watchdog/run")
    assert r.json()["fired"] is False
    assert len(env.fake.calls) == 0            # silence == watching and fine


def test_watchdog_stays_green_after_routine_410_prune(env):
    # A dead-token 410 is routine housekeeping, NOT a chain break — it must not fire a
    # spurious critical when heartbeat + HA are fresh.
    _register_push(env, "phoneA", "mom", "dead-tok")
    env.http.post("/v1/heartbeat", json={"parent_id": "mom", "ha_ok": True})
    env.fake.next_status, env.fake.next_reason = 410, "Unregistered"
    env.http.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b"})
    r = env.http.post("/v1/watchdog/run")
    assert r.json()["fired"] is False        # prune ≠ chain broken


def test_watchdog_fires_on_push_transport_failure(env):
    # A genuine transport failure (network error → status 0) is the exact silent-failure
    # §7.7 exists to catch — it must fire even with fresh heartbeat + HA.
    _register_push(env, "phoneA", "mom", "tok-mom")
    env.http.post("/v1/heartbeat", json={"parent_id": "mom", "ha_ok": True})
    env.fake.next_status, env.fake.next_reason = 0, "network error"
    env.http.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b"})
    r = env.http.post("/v1/watchdog/run")
    body = r.json()
    assert body["fired"] is True
    assert "undeliverable" in body["reason"]


def test_watchdog_amber_when_degrading(env):
    now = 10_000
    d = env.routing.evaluate_watchdog(
        now=now, last_heartbeat=now - 600, heartbeat_timeout=900,   # 0.67 of timeout
        ha_last_seen=now - 10, ha_timeout=900, owlet_unavailable=False,
        undeliverable=False)
    assert d.fire is False and d.status == "amber" and d.reason == "degrading"


def test_monitoring_status_accessor(env):
    env.http.post("/v1/heartbeat", json={"parent_id": "mom", "ha_ok": True})
    r = env.http.get("/v1/monitoring/status")
    body = r.json()
    assert body["status"] == "green"
    assert body["healthy"] is True
    assert body["last_heartbeat"] is not None
