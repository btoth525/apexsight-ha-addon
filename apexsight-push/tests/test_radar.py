"""Property Radar calibration sync: GET/POST /v1/radar (1.33.0, mounts + face kinds 1.34.0, placed lens + view pitch 1.35.0).

Pairing-gated both ways, junk dropped on save, size-capped, and the house's coordinates (zone.home)
parsed without ever being stored. Coordinates and names here are made up — this repo is public and
no real location or person may appear in it.

Run:  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_radar.py
"""
import asyncio
import importlib
import json
import os
import tempfile

os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_radar_")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
os.environ["APEX_SECRET_KEY"] = "testsecret"
os.environ.pop("SUPERVISOR_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
importlib.reload(m)
from app import db  # noqa: E402

db.init()

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


CODE = os.environ["PAIRING_CODE"]
client = TestClient(m.app)

# Pairing gate, both directions.
check("GET refuses a bad code", client.get("/v1/radar", params={"pairing_code": "nope"}).status_code == 403)
check("POST refuses a bad code",
      client.post("/v1/radar", json={"pairing_code": "nope", "config": {}}).status_code == 403)

# Nothing saved yet; no Supervisor token means no HA extras.
r = client.get("/v1/radar", params={"pairing_code": CODE}).json()
check("empty to start", r.get("config") is None and r.get("home") is None)
check("no phone tracking in the answer", "people" not in r)

config = {
    "cameras": {
        "Front_Driveway": {"mount": {"lat": 1.0003, "lon": 2.0003, "height": 3.1}, "points": [
            {"x": 0.1, "y": 0.9, "lat": 1.0001, "lon": 2.0001},
            {"x": 0.5, "y": 0.5, "lat": 1.0002, "lon": 2.0002},
            {"x": 1.5, "y": 0.5, "lat": 1.0, "lon": 2.0},      # outside the frame: dropped
            {"x": 0.2, "y": 0.2, "lat": 95.0, "lon": 2.0},     # impossible latitude: dropped
            {"x": "a", "y": 0.2, "lat": 1.0, "lon": 2.0},      # not a number: dropped
        ]},
        "../etc": {"points": [{"x": 0.1, "y": 0.1, "lat": 1.0, "lon": 2.0}]},  # bad name: dropped
        "doorbell": "nonsense",                                               # not an object: dropped
        "Side_Gate": {"points": [], "mount": {"lat": 1.0, "lon": 2.0}},          # no height: default
        "Garage": {"points": [], "mount": {"lat": 1.0, "lon": 2.0, "height": 99},   # absurd height: default
                   "heading": 135.5, "fov": 100, "aim": 12, "icon": "video.doorbell.fill"},
        "Backyard_Wide": {"points": [], "heading": 400, "fov": 5, "aim": -3, "icon": "../x"},  # all out of range
    },
    "faces": {"Alex": "adult", "Kid (Small)": "child", "../x": "child", "Sam": "giant", "Lee": 3},
    "home": {"lat": 1.00005, "lon": 2.00007},
    "view": {"lat": 1.0, "lon": 2.0, "distance": 150, "heading": 30, "pitch": 62},
}
saved = client.post("/v1/radar", json={"pairing_code": CODE, "config": config})
check("POST accepts a calibration", saved.status_code == 200)
clean = saved.json().get("config", {})
cams = clean.get("cameras", {})
check("only the real cameras are kept", sorted(cams) == ["Backyard_Wide", "Front_Driveway", "Garage", "Side_Gate"])
check("the placed lens is kept", {k: cams["Garage"].get(k) for k in ("heading", "fov", "aim", "icon")}
      == {"heading": 135.5, "fov": 100.0, "aim": 12.0, "icon": "video.doorbell.fill"})
check("an out-of-range lens and a bad icon are dropped",
      not any(k in cams["Backyard_Wide"] for k in ("heading", "fov", "aim", "icon")))
check("only in-frame, real points are kept", len(cams["Front_Driveway"]["points"]) == 2)
check("the mount is kept", cams["Front_Driveway"].get("mount") == {"lat": 1.0003, "lon": 2.0003, "height": 3.1})
check("a mount without a height gets the default", cams["Side_Gate"]["mount"]["height"] == 2.7)
check("an absurd mount height gets the default", cams["Garage"]["mount"]["height"] == 2.7)
check("only valid face kinds are kept", clean.get("faces") == {"Alex": "adult", "Kid (Small)": "child"})
check("the corrected house spot is kept", clean.get("home") == {"lat": 1.00005, "lon": 2.00007})
check("the saved map view is kept", clean.get("view", {}).get("heading") == 30.0 and clean["view"].get("pitch") == 62.0)
check("stamped with the save time", isinstance(clean.get("updated_at"), float))

back = client.get("/v1/radar", params={"pairing_code": CODE}).json()
check("GET returns what was saved", back.get("config", {}).get("cameras") == cams)

# A phone on an older app saves without the house's spot: the stored correction stays.
older = client.post("/v1/radar", json={"pairing_code": CODE, "config": {"cameras": {}, "faces": {"Alex": "adult"}}})
check("a save without home keeps the stored house spot", older.json().get("config", {}).get("home") == {"lat": 1.00005, "lon": 2.00007})
moved = client.post("/v1/radar", json={"pairing_code": CODE, "config": {"cameras": {}, "home": {"lat": 1.2, "lon": 2.2}}})
check("a save with home replaces it", moved.json().get("config", {}).get("home") == {"lat": 1.2, "lon": 2.2})

# Size cap.
huge = {"cameras": {"Front_Driveway": {"points": [], "pad": "x" * (70 * 1024)}}}
check("an oversized save is refused",
      client.post("/v1/radar", json={"pairing_code": CODE, "config": huge}).status_code == 413)


# The house's coordinates come from zone.home through the Supervisor; junk is ignored.
class FakeResp:
    status_code = 200
    text = json.dumps({"lat": 1.5, "lon": 2.5})


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        assert url.endswith("/api/template")
        return FakeResp()


m.SUPERVISOR_TOKEN = "test-token"
m._radar_home_cache["at"] = 0.0
real_client = m.httpx.AsyncClient
m.httpx.AsyncClient = FakeClient
try:
    check("home coordinates parsed", asyncio.run(m._radar_home()) == {"lat": 1.5, "lon": 2.5})
    FakeResp.text = json.dumps({"lat": None, "lon": "x"})
    m._radar_home_cache["at"] = 0.0
    check("missing coordinates give no home", asyncio.run(m._radar_home()) is None)
finally:
    m.httpx.AsyncClient = real_client

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
