"""Security regression tests for the ApexSight push relay.

Run:  PYTHONPATH=. PAIRING_CODE=APEX-PLEX-5250 python3 tests/test_security.py
Exercises the pairing-gate authz, the alert-suppress endpoint gating, the body-size cap,
the constant-time mode-extras check, the REQUIRE_MODE_CODE occupancy-oracle gate, and the
trusted-proxy / in-house-bridge request classification.
"""
import os, tempfile, importlib
os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_")
os.environ.setdefault("PAIRING_CODE", "APEX-PLEX-5250")
os.environ["APEX_SECRET_KEY"] = "testsecret"
from fastapi.testclient import TestClient
from starlette.requests import Request

ok = []
def check(name, cond):
    ok.append(bool(cond)); print(("PASS" if cond else "FAIL"), name)

def fake_request(peer_host, headers=None):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "client": (peer_host, 0), "headers": hdrs})

# ---- default (REQUIRE_MODE_CODE off): gate/muted/body/extras -----------------
import app.main as m
importlib.reload(m)
with TestClient(m.app) as c:
    check("gate wrong code -> 403", c.post("/v1/gate", json={"pairing_code":"WRONG","disarmed":True,"snoozed_until":0}).status_code == 403)
    check("gate correct (lowercased) -> 200", c.post("/v1/gate", json={"pairing_code":"apex-plex-5250","disarmed":False,"snoozed_until":0}).status_code == 200)
    check("muted-cameras wrong code -> 403", c.post("/v1/muted-cameras", json={"pairing_code":"WRONG","muted":["Garage"]}).status_code == 403)
    check("muted-cameras correct -> 200", c.post("/v1/muted-cameras", json={"pairing_code":"APEX-PLEX-5250","muted":["Garage"]}).status_code == 200)
    r = c.get("/v1/mode", params={"pairing_code":"APEX-PLEX-5250"})
    check("mode correct code -> gate extras present", r.status_code==200 and "disarmed" in r.json())
    r = c.get("/v1/mode", params={"pairing_code":"NOPE"})
    check("mode wrong code -> extras omitted", r.status_code==200 and "disarmed" not in r.json())
    check("mode base open when flag off (default)", "mode" in c.get("/v1/mode").json())
    check("oversized body -> 413", c.post("/v1/gate", headers={"content-length": str(20*1024*1024)}, json={"pairing_code":"x"}).status_code == 413)
    check("bad content-length -> 400", c.post("/v1/gate", headers={"content-length":"nope"}, content=b"{}").status_code == 400)

# ---- REQUIRE_MODE_CODE on: base response now gated --------------------------
os.environ["REQUIRE_MODE_CODE"] = "true"
importlib.reload(m)
with TestClient(m.app) as c:
    check("flag ON: no code -> 403 (oracle closed)", c.get("/v1/mode").status_code == 403)
    check("flag ON: wrong code -> 403", c.get("/v1/mode", params={"pairing_code":"NOPE"}).status_code == 403)
    check("flag ON: correct code -> 200 + mode", (lambda r: r.status_code==200 and "mode" in r.json())(c.get("/v1/mode", params={"pairing_code":"APEX-PLEX-5250"})))
del os.environ["REQUIRE_MODE_CODE"]
importlib.reload(m)

# ---- R2: trusted-proxy / in-house-bridge classification --------------------
check("public peer is NOT trusted proxy", not m._from_trusted_proxy(fake_request("8.8.8.8")))
check("loopback peer IS trusted proxy", m._from_trusted_proxy(fake_request("127.0.0.1")))
check("private peer (docker) IS trusted proxy", m._from_trusted_proxy(fake_request("172.30.33.5")))
check("spoofed cf-header from public peer is ignored (real_ip = socket peer)",
      m._real_ip(fake_request("8.8.8.8", {"cf-connecting-ip":"10.0.0.1"})) == "8.8.8.8")
check("cf-header from tunnel (private peer) is honored",
      m._real_ip(fake_request("172.30.33.5", {"cf-connecting-ip":"8.8.8.8"})) == "8.8.8.8")
check("loopback + no header = in-house bridge (exempt)", m._is_inhouse_bridge(fake_request("127.0.0.1")))
check("loopback + forwarding header != bridge", not m._is_inhouse_bridge(fake_request("127.0.0.1", {"x-forwarded-for":"1.2.3.4"})))
check("public peer != in-house bridge", not m._is_inhouse_bridge(fake_request("8.8.8.8")))

# ---- atomic mode-request seq: distinct under concurrency (no dropped arm/disarm) ----------
import threading as _threading
import app.db as _db
_db.set_config("mode_request_seq", "0")
_out, _lock = [], _threading.Lock()
def _seq_worker():
    v = _db.next_mode_request_seq()
    with _lock:
        _out.append(v)
_ts = [_threading.Thread(target=_seq_worker) for _ in range(20)]
[t.start() for t in _ts]
[t.join() for t in _ts]
check("mode-request seq is distinct under 20 concurrent callers", len(set(_out)) == 20)

print("\n%d/%d passed" % (sum(ok), len(ok)))
raise SystemExit(0 if all(ok) else 1)
