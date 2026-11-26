"""Regression tests for the Tapo telephoto pan/tilt routes (1.32.0).

Run:  PYTHONPATH=. python3 tests/test_ptz.py

Covers the calibration math (tap-to-aim must land where the aim box says it will), the dead-man
lease (a joystick that goes quiet must stop the lens), the pairing gate, the camera allowlist, and
the ONVIF parsing. The camera itself is stubbed; no network.
"""
import asyncio
import os
import tempfile

os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
os.environ["APEX_SECRET_KEY"] = "testsecret"
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import ptz  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


CODE = "APEX-TEST-0000"

# ---- calibration math ----
for name, cal in ptz.CALIBRATION.items():
    # Round trip across the middle of the wide frame: aim at a point, project back, land on it.
    worst = 0.0
    for x in (0.3, 0.45, 0.6, 0.75):
        for y in (0.3, 0.45, 0.6):
            p, t = ptz.solve(cal, x, y)
            px, py, _ = ptz.project(cal, p, t)
            if cal["pan"][0] < p < cal["pan"][1] and cal["tilt"][0] < t < cal["tilt"][1]:
                worst = max(worst, abs(px - x), abs(py - y))
    check(f"{name}: tap-to-aim lands within 1% of the tapped point", worst < 0.01)
    p, t = ptz.solve(cal, -5.0, 9.0)
    check(f"{name}: an unreachable point clamps inside the calibrated range",
          cal["pan"][0] <= p <= cal["pan"][1] and cal["tilt"][0] <= t <= cal["tilt"][1])
    box = ptz.aim_box(cal, sum(cal["pan"]) / 2, sum(cal["tilt"]) / 2)
    check(f"{name}: aim box is square in normalized units (both lenses 16:9)", box["w"] == box["h"])
    check(f"{name}: aim box footprint is a sane fraction of the wide frame", 0.2 < box["w"] < 0.7)

# Measured anchor points from the 2026-11-25 calibration (the lens's real resting positions).
x, y, _ = ptz.project(ptz.CALIBRATION["Backyard_PTZ"], 0.296066, -0.342282)
check("Backyard rest position projects onto the patio (measured 0.846, 0.72)", abs(x - 0.846) < 0.02 and abs(y - 0.72) < 0.02)
x, y, _ = ptz.project(ptz.CALIBRATION["Living_Room_PTZ"], 0.006211, -0.798658)
check("Living room rest position projects onto the couch (measured ~0.58, 0.64)", abs(x - 0.58) < 0.03 and abs(y - 0.64) < 0.03)
check("Living room is mounted mirrored: more pan moves the view LEFT",
      ptz.project(ptz.CALIBRATION["Living_Room_PTZ"], 0.2, -0.5)[0] < ptz.project(ptz.CALIBRATION["Living_Room_PTZ"], -0.2, -0.5)[0])

# ---- parsing / cleaning ----
xml = ('<tptz:GetPresetsResponse><tptz:Preset token="1"><tt:Name>Viewpoint 1</tt:Name></tptz:Preset>'
       '<tptz:Preset token="7"><tt:Name>Swing set</tt:Name><tt:PTZPosition/></tptz:Preset></tptz:GetPresetsResponse>')
check("presets parse token + name", ptz.parse_presets(xml) == [{"token": "1", "name": "Viewpoint 1"}, {"token": "7", "name": "Swing set"}])
check("preset names drop markup characters", ptz.clean_preset_name("<b>Gate</b> & path") == "bGateb path")
check("preset names are capped at 24 chars", len(ptz.clean_preset_name("x" * 80)) == 24)
check("credentials are XML-escaped into the SOAP header", "&lt;" in ptz._security("a<b", "pw"))

RAW = """cameras:
  Backyard_Wide:
    onvif:
      host: 10.0.0.9
      user: wrong
      password: wrong
  Backyard_PTZ:
    # the tele lens
    enabled: true
    detect:
      enabled: false
    onvif:
      host: 192.0.2.28
      port: 2020
      user: Brandon
      password: "p@ss: w#rd"  # quoted

  Living_Room_PTZ:
    enabled: true
"""
o = ptz.parse_onvif_block(RAW, "Backyard_PTZ")
check("onvif block is read from the raw YAML of THAT camera only",
      o == {"host": "192.0.2.28", "port": "2020", "user": "Brandon", "password": "p@ss: w#rd"})
check("a camera with no onvif block yields None", ptz.parse_onvif_block(RAW, "Living_Room_PTZ") is None)
check("a camera missing from the config yields None", ptz.parse_onvif_block(RAW, "Garage") is None)


# ---- routes with a stubbed camera ----
class FakeCam:
    def __init__(self):
        self.calls = []
        self.pos = (0.1, -0.4)
        self.preset_list = [{"token": "1", "name": "Viewpoint 1"}]

    async def status(self):
        return self.pos

    async def continuous(self, vx, vy):
        self.calls.append(("move", round(vx, 3), round(vy, 3)))

    async def stop(self):
        self.calls.append(("stop",))

    async def absolute(self, p, t):
        self.calls.append(("abs", p, t))
        self.pos = (p, t)

    async def presets(self):
        return list(self.preset_list)

    async def set_preset(self, name):
        self.preset_list.append({"token": "9", "name": name})
        self.calls.append(("set", name))
        return "9"

    async def goto_preset(self, token):
        self.calls.append(("goto", token))

    async def remove_preset(self, token):
        self.preset_list = [p for p in self.preset_list if p["token"] != token]
        self.calls.append(("rm", token))


cam = FakeCam()


async def fake_blocks():
    return {"Backyard_PTZ": cam}

ptz._load_onvif_blocks = fake_blocks
ptz.MOVE_LEASE_SECONDS = 0.3
ptz._creds["at"] = 0.0

with TestClient(m.app) as c:
    check("list without the pairing code -> 403", c.get("/v1/ptz", params={"pairing_code": "NOPE"}).status_code == 403)
    r = c.get("/v1/ptz", params={"pairing_code": CODE})
    check("list names only cameras Frigate can drive, with their wide lens",
          r.status_code == 200 and r.json()["cameras"] == [{"camera": "Backyard_PTZ", "wide": "Backyard_Wide", "aim": True}])
    check("a camera outside the allowlist -> 404",
          c.get("/v1/ptz/Front_Driveway/state", params={"pairing_code": CODE}).status_code == 404)
    check("a calibrated camera with no onvif block in Frigate -> 404",
          c.get("/v1/ptz/Living_Room_PTZ/state", params={"pairing_code": CODE}).status_code == 404)

    r = c.get("/v1/ptz/Backyard_PTZ/state", params={"pairing_code": CODE, "presets": "true"})
    j = r.json()
    check("state reports position, aim box and presets", r.status_code == 200 and j["pan"] == 0.1 and "aim" in j and j["presets"][0]["name"] == "Viewpoint 1")

    check("move with the wrong code -> 403", c.post("/v1/ptz/Backyard_PTZ/move", json={"pairing_code": "x", "x": 1}).status_code == 403)
    cam.calls.clear()
    r = c.post("/v1/ptz/Backyard_PTZ/move", json={"pairing_code": CODE, "x": 2.5, "y": -0.4})
    check("move clamps velocity to [-1, 1] and sends a diagonal", r.json()["moving"] and cam.calls[-1] == ("move", 1.0, -0.4))
    import time as _t
    _t.sleep(0.6)
    check("dead-man lease: a joystick that goes quiet stops the lens", cam.calls[-1] == ("stop",))

    cam.calls.clear()
    c.post("/v1/ptz/Backyard_PTZ/move", json={"pairing_code": CODE, "x": 0.5, "y": 0})
    _t.sleep(0.15)
    c.post("/v1/ptz/Backyard_PTZ/move", json={"pairing_code": CODE, "x": 0.5, "y": 0})
    _t.sleep(0.15)
    c.post("/v1/ptz/Backyard_PTZ/move", json={"pairing_code": CODE, "x": 0.5, "y": 0})
    check("a held stick renews its lease (no stop while it keeps talking)", ("stop",) not in cam.calls)
    r = c.post("/v1/ptz/Backyard_PTZ/move", json={"pairing_code": CODE, "x": 0, "y": 0})
    check("releasing the stick stops at once", r.json()["moving"] is False and cam.calls[-1] == ("stop",))
    n = len(cam.calls)
    _t.sleep(0.5)
    check("…and the cancelled lease doesn't fire a second stop later", len(cam.calls) == n)

    r = c.post("/v1/ptz/Backyard_PTZ/aim", json={"pairing_code": CODE, "x": 0.5, "y": 0.5})
    j = r.json()
    check("aim sends an AbsoluteMove to the solved position", cam.calls[-1][0] == "abs" and abs(j["aim"]["x"] - 0.5) < 0.01 and abs(j["aim"]["y"] - 0.5) < 0.01)

    r = c.post("/v1/ptz/Backyard_PTZ/presets", json={"pairing_code": CODE, "name": "  Swing   set "})
    check("saving a spot names it on the camera and returns the new list",
          r.status_code == 200 and r.json()["token"] == "9" and cam.calls[-1] == ("set", "Swing set") and len(r.json()["presets"]) == 2)
    check("an empty spot name -> 400", c.post("/v1/ptz/Backyard_PTZ/presets", json={"pairing_code": CODE, "name": "<>"}).status_code == 400)
    c.post("/v1/ptz/Backyard_PTZ/presets/9/goto", json={"pairing_code": CODE})
    check("go to a spot", cam.calls[-1] == ("goto", "9"))
    r = c.post("/v1/ptz/Backyard_PTZ/presets/9/delete", json={"pairing_code": CODE})
    check("delete a spot and get the trimmed list", cam.calls[-1] == ("rm", "9") and len(r.json()["presets"]) == 1)
    check("preset routes are pairing-gated too",
          c.post("/v1/ptz/Backyard_PTZ/presets/1/delete", json={"pairing_code": "bad"}).status_code == 403)

print(f"\n{sum(ok)}/{len(ok)} passed")
if not all(ok):
    raise SystemExit(1)
