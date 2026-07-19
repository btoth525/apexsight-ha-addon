"""Server-side sync tests: dedupe, LWW, pull cursor, auth. Fast, no network."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("LULLA_DATA_DIR", tmp)
    monkeypatch.setenv("PAIRING_CODE", "LULLA-TEST-0001")
    # Import AFTER env is set so config picks up the temp dir + pairing code.
    import importlib
    from app import config as cfg
    importlib.reload(cfg)
    from app import db as dbmod
    importlib.reload(dbmod)
    from app import main as mainmod
    importlib.reload(mainmod)
    dbmod.init()
    return TestClient(mainmod.app)


def _register(client, device_id):
    r = client.post("/v1/register", json={
        "pairing_code": "LULLA-TEST-0001", "device_id": device_id, "device_name": device_id})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _rec(id_, updated_at, created_by, note, tomb=False):
    return {"type": "LogEvent", "id": id_, "updated_at": updated_at,
            "created_by": created_by, "is_tombstoned": tomb, "payload": f'{{"note":"{note}"}}'}


def test_register_requires_pairing_code(client):
    r = client.post("/v1/register", json={"pairing_code": "WRONG", "device_id": "A"})
    assert r.status_code == 403


def test_sync_requires_bearer(client):
    assert client.get("/v1/sync/pull").status_code == 401
    assert client.post("/v1/sync/push", json={"records": []}).status_code == 401


def test_create_on_A_pulls_on_B(client):
    ta, tb = _register(client, "A"), _register(client, "B")
    client.post("/v1/sync/push", json={"records": [_rec("id1", 100.0, "A", "hi")]}, headers=_auth(ta))
    pull = client.get("/v1/sync/pull?since=0", headers=_auth(tb)).json()
    ids = [r["id"] for r in pull["records"]]
    assert "id1" in ids
    assert pull["cursor"] >= 1


def test_dedupe_and_lww(client):
    ta = _register(client, "A")
    client.post("/v1/sync/push", json={"records": [_rec("id1", 100.0, "A", "first")]}, headers=_auth(ta))
    # newer wins
    client.post("/v1/sync/push", json={"records": [_rec("id1", 200.0, "B", "second")]}, headers=_auth(ta))
    # older loses (no-op)
    r = client.post("/v1/sync/push", json={"records": [_rec("id1", 50.0, "C", "stale")]}, headers=_auth(ta))
    assert r.json()["applied"] == 0
    pull = client.get("/v1/sync/pull?since=0", headers=_auth(ta)).json()
    id1 = [r for r in pull["records"] if r["id"] == "id1"]
    assert len(id1) == 1                       # dedupe: one row
    assert "second" in id1[0]["payload"]       # LWW: newer won


def test_pull_cursor_is_incremental(client):
    ta = _register(client, "A")
    client.post("/v1/sync/push", json={"records": [_rec("id1", 100.0, "A", "a")]}, headers=_auth(ta))
    first = client.get("/v1/sync/pull?since=0", headers=_auth(ta)).json()
    cur = first["cursor"]
    client.post("/v1/sync/push", json={"records": [_rec("id2", 110.0, "A", "b")]}, headers=_auth(ta))
    second = client.get(f"/v1/sync/pull?since={cur}", headers=_auth(ta)).json()
    ids = [r["id"] for r in second["records"]]
    assert ids == ["id2"]                       # only the new one since the cursor


def test_tombstone_syncs(client):
    ta, tb = _register(client, "A"), _register(client, "B")
    client.post("/v1/sync/push", json={"records": [_rec("id1", 100.0, "A", "live")]}, headers=_auth(ta))
    client.post("/v1/sync/push", json={"records": [_rec("id1", 200.0, "A", "live", tomb=True)]}, headers=_auth(ta))
    pull = client.get("/v1/sync/pull?since=0", headers=_auth(tb)).json()
    id1 = [r for r in pull["records"] if r["id"] == "id1"][0]
    assert id1["is_tombstoned"] is True


def test_tie_break_deterministic(client):
    ta = _register(client, "A")
    client.post("/v1/sync/push", json={"records": [_rec("id1", 100.0, "phoneA", "a")]}, headers=_auth(ta))
    # same updated_at, higher created_by wins
    client.post("/v1/sync/push", json={"records": [_rec("id1", 100.0, "phoneB", "b")]}, headers=_auth(ta))
    pull = client.get("/v1/sync/pull?since=0", headers=_auth(ta)).json()
    id1 = [r for r in pull["records"] if r["id"] == "id1"][0]
    assert "b" in id1["payload"]
    # and it does NOT flip back for a lower created_by at the same time
    r = client.post("/v1/sync/push", json={"records": [_rec("id1", 100.0, "phoneA", "c")]}, headers=_auth(ta))
    assert r.json()["applied"] == 0
