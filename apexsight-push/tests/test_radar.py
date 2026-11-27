"""Property Radar calibration sync: GET/POST /v1/radar (1.33.0).

Pairing-gated both ways, junk dropped on save, size-capped, and the Home Assistant extras (zone.home,
Bermuda phone areas) parsed without ever being stored. Coordinates here are made up — this repo is
public and no real location may appear in it.

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
check("empty to start", r.get("config") is None and r.get("home") is None and r.get("people") == [])

config = {
    "cameras": {
        "Front_Driveway": {"area": "Front Driveway", "points": [
            {"x": 0.1, "y": 0.9, "lat": 1.0001, "lon": 2.0001},
            {"x": 0.5, "y": 0.5, "lat": 1.0002, "lon": 2.0002},
            {"x": 1.5, "y": 0.5, "lat": 1.0, "lon": 2.0},      # outside the frame: dropped
            {"x": 0.2, "y": 0.2, "lat": 95.0, "lon": 2.0},     # impossible latitude: dropped
            {"x": "a", "y": 0.2, "lat": 1.0, "lon": 2.0},      # not a number: dropped
        ]},
        "../etc": {"points": [{"x": 0.1, "y": 0.1, "lat": 1.0, "lon": 2.0}]},  # bad name: dropped
        "doorbell": "nonsense",                                               # not an object: dropped
    },
    "view": {"lat": 1.0, "lon": 2.0, "distance": 150, "heading": 30},
}
saved = client.post("/v1/radar", json={"pairing_code": CODE, "config": config})
check("POST accepts a calibration", saved.status_code == 200)
clean = saved.json().get("config", {})
cams = clean.get("cameras", {})
check("only the real camera is kept", list(cams) == ["Front_Driveway"])
check("only in-frame, real points are kept", len(cams["Front_Driveway"]["points"]) == 2)
check("the camera's HA area is kept", cams["Front_Driveway"].get("area") == "Front Driveway")
check("the saved map view is kept", clean.get("view", {}).get("heading") == 30.0)
check("stamped with the save time", isinstance(clean.get("updated_at"), float))

back = client.get("/v1/radar", params={"pairing_code": CODE}).json()
check("GET returns what was saved", back.get("config", {}).get("cameras") == cams)

# Size cap.
huge = {"cameras": {"Front_Driveway": {"points": [], "pad": "x" * (70 * 1024)}}}
check("an oversized save is refused",
      client.post("/v1/radar", json={"pairing_code": CODE, "config": huge}).status_code == 413)


# HA extras: the template answer is parsed, names lose " Area", away phones have no area.
class FakeResp:
    status_code = 200
    text = json.dumps({
        "home": {"lat": 1.5, "lon": 2.5},
        "people": [
            {"id": "sensor.alex_phone_area", "name": "Alexs Iphone Area", "area": "Front Driveway", "since": 1000.0},
            {"id": "sensor.sam_phone_area", "name": "Sams Iphone Area", "area": "unknown", "since": 1000.0},
        ],
    })


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
m._radar_ctx_cache["at"] = 0.0
real_client = m.httpx.AsyncClient
m.httpx.AsyncClient = FakeClient
try:
    home, people = asyncio.run(m._radar_ha_context())
finally:
    m.httpx.AsyncClient = real_client
check("home coordinates parsed", home == {"lat": 1.5, "lon": 2.5})
check("phone names lose the ' Area' suffix", [p["name"] for p in people] == ["Alexs Iphone", "Sams Iphone"])
check("a phone in an area keeps it", people[0]["area"] == "Front Driveway")
check("an unknown area becomes none", people[1]["area"] is None)

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
