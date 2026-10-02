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

    # The control routes now need a household token (relay 1.10.0); the test client carries one.
    test_token = dbmod.register_device(mainmod.household_id(), "test-runner", None)
    ns = types.SimpleNamespace(
        http=TestClient(mainmod.app, headers={"Authorization": f"Bearer {test_token}"}), db=dbmod, apns=apnsmod, routing=routingmod,
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


def test_quiet_hours_no_longer_suppresses(env, monkeypatch):
    # For a baby tracker, "Mom fed her at 3am" is exactly what the other parent wants to see —
    # so quiet hours must NOT hold back a routine log notification overnight anymore.
    _register_push(env, "phoneA", "mom", "tok-mom")
    monkeypatch.setattr(env.main, "_now_local_minutes", lambda: 3 * 60)  # 03:00, deep in old quiet window
    monkeypatch.setattr(env.config, "QUIET_HOURS_START", "22:00")
    monkeypatch.setattr(env.config, "QUIET_HOURS_END", "07:00")
    # non-urgent at 3am → still delivered.
    r = env.http.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b"})
    assert r.json()["suppressed"] is False
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


# ---- sleep Live Activity content-state --------------------------------------------------
# CLAUDE.md non-negotiable: the content-state must round-trip the Swift ContentState EXACTLY.
# A mismatch throws nowhere visible — the Lock Screen card just silently stops updating. The
# Swift side asserts the same shape in LullaDataTests/OwletSleepContentStateTests.swift; these
# two tests are a matched pair and must be changed together.

def test_sleep_content_state_matches_the_swift_shape():
    from app.main import _sleep_content_state
    state = _sleep_content_state(sleep_started="2026-09-06T01:55:26Z",
                                 stage_since="2026-09-06T02:10:00Z",
                                 stage="deep_sleep", vitals={"bpm": 129, "spo2": 99})
    assert set(state) == {"sleepStartedAt", "stageSince", "stageLabel",
                          "liveStage", "liveStageSince", "bpm", "spo2"}
    assert state["stageLabel"] == "Deep Sleep"        # raw sock vocabulary made readable
    assert state["sleepStartedAt"].endswith("Z")      # Swift decodes with .iso8601


def test_sleep_content_state_end_payload_is_all_nulls_not_missing_keys():
    """Swift's synthesized decoder tolerates an explicit null for an Optional, but a MISSING key
    would break a non-optional if this struct ever gains one. Keep every key present."""
    from app.main import _sleep_content_state
    state = _sleep_content_state(sleep_started="2026-09-06T01:55:26Z",
                                 stage_since="2026-09-06T02:10:00Z", stage=None, vitals={})
    assert set(state) == {"sleepStartedAt", "stageSince", "stageLabel",
                          "liveStage", "liveStageSince", "bpm", "spo2"}
    assert state["stageLabel"] is None and state["bpm"] is None and state["spo2"] is None
    assert state["liveStage"] is None and state["liveStageSince"] is None


def test_live_stage_carries_the_raw_transfer_window_fields():
    """The raw fast-path (transfer window): liveStage flips the instant the sock says deep,
    while the confirmed stageLabel/stageSince stay put. They must be independent fields."""
    from app.main import _sleep_content_state
    state = _sleep_content_state(
        sleep_started="2026-09-06T01:55:26Z", stage_since="2026-09-06T02:00:00Z",
        stage="light_sleep", vitals={"bpm": 120}, live_stage="deep_sleep",
        live_stage_since="2026-09-06T02:12:00Z")
    assert state["stageLabel"] == "Light Sleep"       # confirmed, stable
    assert state["liveStage"] == "Deep Sleep"         # raw, instant
    assert state["liveStageSince"] == "2026-09-06T02:12:00Z"


# ---- relay 1.10.0 hardening ----------------------------------------------------

def test_unauthenticated_push_only_while_an_old_build_is_registered(env):
    anon = TestClient(env.main.app)
    _register_push(env, "phoneA", "mom", "tok-mom")          # no app_version → counts as old build
    r = anon.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b",
                                    "household": "someone-else", "interruption_level": "critical",
                                    "data": {"aps": {"alert": "spoof"}, "owlet": {"bpm": 1}, "route": "timeline"}})
    assert r.status_code == 200, r.text
    sent = env.fake.calls[-1]["body"]
    assert sent["aps"]["alert"]["title"] == "t"              # our header, not the caller's
    assert "owlet" not in sent and sent.get("route") == "timeline"
    assert sent["aps"].get("interruption-level") != "critical"
    # Once every phone runs build 42+, a token is required.
    env.db.upsert_push_device(device_token="tok-mom", household=env.main.household_id(), parent_id="mom",
                              env="prod", app_version="42")
    assert anon.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b"}).status_code == 401
    assert env.http.post("/v1/push", json={"event": "event.logged", "title": "t", "body": "b"}).status_code == 200


def test_control_routes_need_a_token(env):
    anon = TestClient(env.main.app)
    for path, body in [("/v1/test", {}), ("/v1/watchdog/run", None), ("/v1/heartbeat", {}),
                       ("/v1/activity/start", {}), ("/v1/activity/update", {}), ("/v1/activity/end", {})]:
        r = anon.post(path, json=body) if body is not None else anon.post(path)
        assert r.status_code == 401, path
    assert anon.get("/v1/monitoring/status").status_code == 401
    h = anon.get("/healthz").json()
    assert "devices" not in h and "households" not in h


def test_reregistering_keeps_the_same_sync_token(env):
    a = _register_push(env, "phoneA", "mom", "tok-1")["token"]
    b = _register_push(env, "phoneA", "mom", "tok-2")["token"]
    assert a == b
    assert env.http.get("/v1/sync/pull", headers={"Authorization": f"Bearer {a}"}).status_code == 200


def test_household_survives_a_pairing_code_change(env, monkeypatch):
    hid = env.main.household_id()
    monkeypatch.setattr(env.config, "PAIRING_CODE", "LULLA-NEW0-CODE")
    assert env.main.household_id() == hid
    r = env.http.post("/v1/register", json={"pairing_code": "LULLA-NEW0-CODE", "device_id": "p2"})
    assert r.status_code == 200 and r.json()["household"] == hid
    assert env.http.post("/v1/register", json={"pairing_code": "LULLA-TEST-0001", "device_id": "p3"}).status_code == 403


def test_sync_push_silently_wakes_the_other_phone(env):
    a = _register_push(env, "phoneA", "phoneA", "tok-a")["token"]
    _register_push(env, "phoneB", "phoneB", "tok-b")
    env.main._last_poke.clear()
    rec = {"type": "LogEvent", "id": "E1", "updated_at": 100.0, "created_by": "phoneA",
           "is_tombstoned": False, "payload": "{}"}
    r = env.http.post("/v1/sync/push", json={"records": [rec]}, headers={"Authorization": f"Bearer {a}"})
    assert r.status_code == 200 and r.json()["applied"] == 1
    import time as _t
    for _ in range(50):
        if any(c["body"].get("event") == "sync.refresh" for c in env.fake.calls): break
        _t.sleep(0.02)
    pokes = [c for c in env.fake.calls if c["body"].get("event") == "sync.refresh"]
    assert len(pokes) == 1 and pokes[0]["url"].endswith("tok-b")      # never the phone that wrote it
    assert pokes[0]["body"]["aps"] == {"content-available": 1}


# ---- owlet.refresh payload size + sleep anchor (1.11.0) -----------------------------------

from app import sleep_history as _sh                      # noqa: E402

_T0 = 1_788_000_000.0
# A realistic owlet part, so the base size of the payload is honest.
_OWLET = {"bpm": 142, "spo2": 98, "battery_pct": 63, "sock_on": True,
          "sleep_state": "light_sleep", "sleep_class": "asleep",
          "asleep_since": "2026-09-28T02:10:00Z", "read_at": "2026-09-28T11:22:33Z"}


def _session(segments):
    return _sh.OwletSession(
        start=segments[0].start, end=segments[-1].end, asleep_minutes=400, light_minutes=250,
        deep_minutes=150, awake_minutes=240, wakings=8, longest_stretch_minutes=90,
        segments=segments)


def _busy_night(n_bands, seed=3):
    """`n_bands` contiguous 1–4 minute runs with a real (>=5 min) waking every 25 bands."""
    import random
    rng = random.Random(seed)
    out, t, prev = [], _T0, None
    for i in range(n_bands):
        if i % 25 == 12 and prev != "awake":
            kind, minutes = "awake", rng.randint(5, 8)
        else:
            kind = rng.choice([k for k in ("light_sleep", "deep_sleep", "awake") if k != prev])
            minutes = rng.randint(1, 4)
        out.append(_sh.Segment(kind, t, t + minutes * 60))
        t += minutes * 60
        prev = kind
    return out


def test_encode_payload_is_compact_and_is_what_gets_sent(env):
    p = {"aps": {"content-available": 1}, "sleep": {"bands": [{"kind": 1, "start": 0, "end": 60}]}}
    body = env.apns.encode_payload(p)
    assert ", " not in body and ": " not in body
    assert json.loads(body) == p
    assert env.apns.payload_size(p) == len(body.encode())

    class RawSender:
        def __init__(self): self.bodies = []
        async def send(self, url, headers, body):
            self.bodies.append(body)
            return 200, "ok"
    raw = RawSender()
    import asyncio
    client = env.apns.APNsClient(sender=raw)
    asyncio.run(client.send_to_token("tok", "prod", p, push_type="background"))
    assert raw.bodies == [body]


def test_a_94_band_night_now_fits_without_merging(env):
    """The 9/21 night (94 bands) went out at 4,428 bytes and APNs rejected the whole push. The
    compact encoding alone brings a night that size under budget with every band intact."""
    night = _busy_night(94)
    p = env.main.build_owlet_refresh_payload(dict(_OWLET), _session(night), now=night[-1].end)
    assert len(p["sleep"]["bands"]) == 94
    assert env.apns.payload_size(p) <= env.main.REFRESH_PAYLOAD_BUDGET
    assert len(json.dumps(p).encode()) > 4096          # ...the old encoding would not have


def test_a_300_band_night_is_merged_to_fit_and_still_covers_the_whole_night(env):
    night = _busy_night(300)
    span = round(night[-1].end - night[0].start)
    p = env.main.build_owlet_refresh_payload(dict(_OWLET), _session(night), now=night[-1].end)
    assert env.apns.payload_size(p) <= env.main.REFRESH_PAYLOAD_BUDGET < env.apns.MAX_PAYLOAD_BYTES
    assert p["owlet"] == _OWLET and p["event"] == "owlet.refresh"
    bands = p["sleep"]["bands"]
    assert 1 < len(bands) < 300
    # Merged, not skipped: first starts at 0, last ends at the span, no holes in between.
    assert bands[0]["start"] == 0 and bands[-1]["end"] == span
    assert all(a["end"] == b["start"] for a, b in zip(bands, bands[1:]))
    assert sum(b["end"] - b["start"] for b in bands) == span
    # Every real waking is still an awake band (kind 2) where it happened.
    for w in (s for s in night if s.band == "awake" and s.seconds >= 300):
        ws, we = w.start - night[0].start, w.end - night[0].start
        holder = [b for b in bands if b["start"] <= ws and we <= b["end"]]
        assert len(holder) == 1 and holder[0]["kind"] == 2
    # The totals are the session's, untouched by merging.
    assert p["sleep"]["wakings"] == 8 and p["sleep"]["asleep_seconds"] == 400 * 60


def test_a_night_that_cannot_shrink_drops_sleep_but_the_owlet_reading_goes_through(env):
    """300 bands alternating 5-minute wakings with 10-minute sleeps: no merge is legal (it would
    paint a waking as sleep), so the summary is dropped — never the whole push."""
    night, t = [], _T0
    for i in range(300):
        kind, minutes = ("awake", 5) if i % 2 else ("light_sleep", 10)
        night.append(_sh.Segment(kind, t, t + minutes * 60))
        t += minutes * 60
    p = env.main.build_owlet_refresh_payload(dict(_OWLET), _session(night), now=t)
    assert "sleep" not in p
    assert p["owlet"] == _OWLET and p["aps"] == {"content-available": 1}
    assert env.apns.payload_size(p) <= env.main.REFRESH_PAYLOAD_BUDGET


def test_no_session_means_no_sleep_key(env):
    p = env.main.build_owlet_refresh_payload(dict(_OWLET), None, now=_T0)
    assert "sleep" not in p and p["owlet"] == _OWLET


def _fake_home_state(env, monkeypatch, *, alerts=None, sleep_state="light_sleep"):
    async def fake_state():
        return {"connected": True, "baby_name": "Ryleigh", "nursery": [],
                "alerts": alerts if alerts is not None else {},
                "vitals": {"bpm": 140, "spo2": 98, "battery_pct": 60, "sock_on": True,
                           "sleep_state": sleep_state}}
    monkeypatch.setattr(env.main.home, "state", fake_state)


def test_home_state_carries_tonights_asleep_since(env, monkeypatch):
    _fake_home_state(env, monkeypatch)
    D = env.main.owlet_log.Debounced
    env.db.set_config("owlet_activity_start", "2026-10-01T02:13:00Z")
    env.db.set_config("owlet_sleep_cls", D(confirmed="asleep").to_json())
    body = env.http.get("/v1/home/state").json()
    assert body["sleep_class"] == "asleep"
    assert body["asleep_since"] == "2026-10-01T02:13:00Z"
    # Awake (or sock off): the stored anchor is last sleep's, so it must NOT be served.
    for cls in ("awake", "nosignal"):
        env.db.set_config("owlet_sleep_cls", D(confirmed=cls).to_json())
        assert env.http.get("/v1/home/state").json()["asleep_since"] is None
    # Asleep with no anchor yet → null, not "".
    env.db.set_config("owlet_sleep_cls", D(confirmed="asleep").to_json())
    env.db.set_config("owlet_activity_start", "")
    assert env.http.get("/v1/home/state").json()["asleep_since"] is None


def test_the_falling_asleep_refresh_push_carries_tonights_anchor_not_last_nights(env, monkeypatch):
    """Drive ONE real poller tick across the confirmed awake→asleep edge. The refresh push goes
    out before the Live Activity block, and the anchor used to be stamped only in the latter."""
    import asyncio
    import time as _t
    _register_push(env, "phoneA", "phoneA", "tok-a")
    _fake_home_state(env, monkeypatch)
    monkeypatch.setattr(env.main.home, "_get", lambda *a, **k: asyncio.sleep(0, result=None))
    D = env.main.owlet_log.Debounced
    now = _t.time()
    stale = env.main.owlet_log.iso_at(now - 20 * 3600)                   # last night's
    env.db.set_config("owlet_activity_start", stale)
    env.db.set_config("owlet_last_poll_ts", str(now - 15))              # no restart gap
    env.db.set_config("owlet_last_real_cls", "awake")
    env.db.set_config("owlet_sleep_cls", D(confirmed="awake", candidate="asleep",
                                           since=now - 400).to_json())   # held > WAKE_HOLD

    class _Stop(BaseException):
        pass

    async def stop(_seconds):
        raise _Stop()
    monkeypatch.setattr(env.main, "asyncio", types.SimpleNamespace(sleep=stop))
    with pytest.raises(_Stop):
        asyncio.run(env.main._owlet_sleep_poller())

    refresh = [c for c in env.fake.calls if c["body"].get("event") == "owlet.refresh"]
    assert len(refresh) == 1
    sent = refresh[0]["body"]["owlet"]
    assert sent["sleep_class"] == "asleep"
    assert sent["asleep_since"] != stale
    import calendar
    anchor = calendar.timegm(_t.strptime(sent["asleep_since"], "%Y-%m-%dT%H:%M:%SZ"))
    assert abs(anchor - (now - env.main.owlet_log.WAKE_HOLD_SECONDS)) < 5
    assert env.db.get_config("owlet_activity_start") == sent["asleep_since"]


def test_sessions_window_drops_a_night_cut_by_since(env, monkeypatch):
    """`days=1` asked at 03:00 used to return the tail of a night that started before the
    window as if it were a whole (short) session."""
    now = 1_788_100_000
    monkeypatch.setattr(env.main, "time", types.SimpleNamespace(time=lambda: now))
    since = now - 86400
    # A night from since-2h to since+6h (straddles the edge), then a nap fully inside.
    for m in range(since - 2 * 3600, since + 6 * 3600, 60):
        env.db.set_sleep_minute(m - m % 60, "light_sleep")
    nap = since + 10 * 3600
    for m in range(nap, nap + 90 * 60, 60):
        env.db.set_sleep_minute(m - m % 60, "deep_sleep")
    body = env.http.get("/v1/home/sleep/sessions?days=1").json()
    starts = [s["start"] for s in body["sessions"]]
    assert starts == [env.main.owlet_log.iso_at(nap - nap % 60)]       # the cut night is gone
    # minute_count still describes the requested window only.
    assert body["minute_count"] == sum(1 for r in env.db.sleep_minutes(since)
                                       if r["minute_ts"] >= since)
