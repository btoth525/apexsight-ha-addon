import os, tempfile
os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_")
os.environ["PAIRING_CODE"] = "APEX-PLEX-5250"
os.environ["APEX_SECRET_KEY"] = "testsecret"
from fastapi.testclient import TestClient
from app.main import app
ok = []
def check(name, cond):
    ok.append(bool(cond)); print(("PASS" if cond else "FAIL"), name)

with TestClient(app) as c:   # context manager runs startup -> db tables created
    r = c.post("/v1/gate", json={"pairing_code":"WRONG-CODE","disarmed":True,"snoozed_until":0})
    check("gate wrong code -> 403", r.status_code == 403)
    r = c.post("/v1/gate", json={"pairing_code":"apex-plex-5250","disarmed":False,"snoozed_until":0})
    check("gate correct code (lowercased) -> 200", r.status_code == 200)

    r = c.post("/v1/muted-cameras", json={"pairing_code":"WRONG","muted":["Garage"]})
    check("muted-cameras wrong code -> 403", r.status_code == 403)
    r = c.post("/v1/muted-cameras", json={"pairing_code":"APEX-PLEX-5250","muted":["Garage"]})
    check("muted-cameras correct -> 200", r.status_code == 200)

    r = c.get("/v1/mode", params={"pairing_code":"APEX-PLEX-5250"})
    check("get_mode correct code returns gate extras", r.status_code==200 and "disarmed" in r.json())
    r = c.get("/v1/mode", params={"pairing_code":"NOPE"})
    check("get_mode wrong code omits gate extras", r.status_code==200 and "disarmed" not in r.json())
    check("get_mode base still returns mode (R1 deferred, expected)", "mode" in c.get("/v1/mode").json())

    r = c.post("/v1/gate", headers={"content-length": str(20*1024*1024)}, json={"pairing_code":"x"})
    check("oversized body -> 413", r.status_code == 413)
    r = c.post("/v1/gate", headers={"content-length": "not-a-number"}, content=b"{}")
    check("bad content-length -> 400", r.status_code == 400)

print("\n%d/%d passed" % (sum(ok), len(ok)))
raise SystemExit(0 if all(ok) else 1)
