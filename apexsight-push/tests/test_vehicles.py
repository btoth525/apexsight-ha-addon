"""Vehicle home/away (1.31.0): identity from LPR sub_labels (known_plates keys), carried across the
two driveway cameras and forward to a re-started parked track; arrival = came in from outside and
parked; departure = left the home zones, confirmed.

Run:  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_vehicles.py
"""
import os
import sys
import tempfile

os.environ.setdefault("APEX_DATA_DIR", tempfile.mkdtemp(prefix="apexveh_"))
sys.path.insert(0, os.path.dirname(__file__))

import smart_alerts  # noqa: E402
from smart_harness import World, check, finish, obj, vehicles_state  # noqa: E402

FD, LPR = "Front_Driveway", "Front_Driveway_LPR"
PARK_FD = [1064, 19, 1394, 284]       # parked spot on the panoramic camera
STREET_FD = [1900, 100, 2000, 180]
PARK_LPR = [693, 0, 1204, 251]
KNOWN = ("Sams_Truck", "Jos_Jeep")


def kinds(w, kind="vehicle_presence"):
    return [p for p in w.posts if p.get("kind") == kind]


def arrive(w, oid, camera, box, sub=None, start=None):
    t = start or w.clock()
    w.send("new", **obj(oid, camera, "car", t, STREET_FD, sub=sub, entered=["street"], current=["street"], moves=1))
    w.tick(5)
    w.send("update", **obj(oid, camera, "car", t, box, sub=sub, entered=["street", "driveway"],
                           current=["driveway"], moves=2))
    w.tick(5)
    w.send("update", **obj(oid, camera, "car", t, box, sub=sub, entered=["street", "driveway"],
                           current=["driveway"], stationary=True, moves=2))
    return t


# ---- 1. names ----
check("display name: possessive", smart_alerts.display_name("Sams_Truck") == "Sam's Truck")
check("display name: 'Grandmas_Car' → Grandma's Car", smart_alerts.display_name("Grandmas_Car") == "Grandma's Car")
check("display name: a single name is untouched", smart_alerts.display_name("Alex") == "Alex")
check("truck gets the pickup emoji", smart_alerts.vehicle_emoji("Sams_Truck") == "\U0001f6fb")

# ---- 2. known vehicles come from known_plates keys, never carriers ----
w = World(known=KNOWN)
check("known set = known_plates keys minus carriers", w.sa.known == set(KNOWN))
st = vehicles_state(w)
check("every known vehicle is listed even before it's seen",
      set(st.get("vehicles", {})) == set(KNOWN) and st["vehicles"]["Sams_Truck"]["status"] == "unknown")

# ---- 3. restart mid-park: silent seed ----
w = World(known=KNOWN)
old = w.clock() - 6 * 3600
w.send("update", **obj("t1", FD, "car", old, PARK_FD, sub="Sams_Truck", entered=["street", "driveway"],
                       current=["driveway"], stationary=True, moves=1))
w.tick(5)
check("an hours-old parked track seeds 'home' silently", not kinds(w)
      and vehicles_state(w)["vehicles"]["Sams_Truck"]["status"] == "home")

# ---- 4. departure: leaves to the street, confirmed ----
w.send("update", **obj("t1", FD, "car", old, STREET_FD, sub="Sams_Truck", entered=["street", "driveway"],
                       current=["street"], moves=2))
w.tick(5)
check("no departure push before it's confirmed", not kinds(w))
w.send("end", **obj("t1", FD, "car", old, STREET_FD, sub="Sams_Truck", entered=["street", "driveway"],
                    current=["street"], moves=3, end=w.clock()))
w.run(70)
d = kinds(w)
check("one departure push", len(d) == 1)
check("…'Sam's Truck left'", d and d[0]["title"].endswith("Sam's Truck left"))
check("…not time-sensitive", d and d[0]["time_sensitive"] is False)
check("…labelled so the app toggle can mute it", d and d[0]["labels"] == ["vehicle_presence"])
check("state is away", vehicles_state(w)["vehicles"]["Sams_Truck"]["status"] == "away")

# ---- 5. arrival: from the street, parks ----
arrive(w, "t2", FD, PARK_FD, sub="Sams_Truck")
w.tick(5)
a = kinds(w)
check("arrival push", len(a) == 2 and a[1]["title"].endswith("Sam's Truck arrived home"))
check("arrival body is '<time> · Front Driveway'", a and a[-1]["body"].endswith("· Front Driveway"))
check("state is home with a parked spot",
      vehicles_state(w)["vehicles"]["Sams_Truck"]["status"] == "home"
      and vehicles_state(w)["vehicles"]["Sams_Truck"].get("spots", {}).get(FD))
for _ in range(20):
    w.tick(5)
    w.send("update", **obj("t2", FD, "car", w.sa.objs["t2"].start, PARK_FD, sub="Sams_Truck",
                           entered=["street", "driveway"], current=["driveway"], stationary=True, moves=2))
check("parked updates don't re-announce", len(kinds(w)) == 2)

# ---- 6. track lost while parked is NOT a departure ----
w.send("end", **obj("t2", FD, "car", w.sa.objs["t2"].start, PARK_FD, sub="Sams_Truck",
                    entered=["street", "driveway"], current=["driveway"], stationary=True, end=w.clock()))
w.run(120)
check("a parked track ending in the driveway is not a departure",
      len(kinds(w)) == 2 and vehicles_state(w)["vehicles"]["Sams_Truck"]["status"] == "home")

# ---- 7. re-started parked track WITHOUT a plate is adopted by its spot, then departs ----
t = w.clock()
w.send("new", **obj("t3", FD, "car", t, [1068, 20, 1392, 286], entered=["driveway"], current=["driveway"],
                    stationary=True))
w.tick(5)
check("an unlabelled track in the truck's parked spot is the truck", w.sa.adopted.get("t3") == "Sams_Truck")
w.send("update", **obj("t3", FD, "car", t, STREET_FD, entered=["driveway", "street"], current=["street"], moves=2))
w.run(70)
check("…and its exit is the truck leaving", len(kinds(w)) == 3 and kinds(w)[-1]["title"].endswith("left"))

# ---- 8. cross-camera: the plate is read on the LPR camera, the parking happens on the wide one ----
w = World(known=KNOWN)
t = w.clock()
w.send("new", **obj("l1", LPR, "car", t, [400, 0, 900, 300], sub="Jos_Jeep", entered=["street"],
                    current=["street"], moves=1))
arrive(w, "f1", FD, PARK_FD, start=t + 3)
w.send("end", **obj("l1", LPR, "car", t, PARK_LPR, sub="Jos_Jeep", entered=["street", "driveway"],
                    current=["driveway"], moves=2, end=w.clock()))
w.run(30)
a = kinds(w)
check("cross-camera: the unlabelled wide-camera track takes the LPR camera's identity",
      w.sa.adopted.get("f1") == "Jos_Jeep")
check("…and the arrival is announced exactly once", len(a) == 1 and "Jo's Jeep arrived home" in a[0]["title"])

# ---- 9. departure cancelled when another camera still sees it parked ----
w = World(known=KNOWN)
old = w.clock() - 3600 * 3
w.send("update", **obj("fa", FD, "car", old, PARK_FD, sub="Sams_Truck", entered=["street", "driveway"],
                       current=["driveway"], stationary=True, moves=1))
w.send("update", **obj("la", LPR, "car", old, PARK_LPR, sub="Sams_Truck", entered=["street", "driveway"],
                       current=["driveway"], stationary=True, moves=1))
w.tick(5)
w.send("update", **obj("fa", FD, "car", old, STREET_FD, sub="Sams_Truck", entered=["street", "driveway"],
                       current=["street"], moves=2))
for _ in range(16):
    w.tick(5)
    w.send("update", **obj("la", LPR, "car", old, PARK_LPR, sub="Sams_Truck", entered=["street", "driveway"],
                           current=["driveway"], stationary=True, moves=1))
check("still parked on the other camera → no departure",
      not kinds(w) and vehicles_state(w)["vehicles"]["Sams_Truck"]["status"] == "home")

# ---- 9b. a parked car's box jittering across the zone line is not a departure ----
w = World(known=KNOWN)
old = w.clock() - 3600 * 3
w.send("update", **obj("lj", LPR, "car", old, PARK_LPR, sub="Sams_Truck", entered=["street", "driveway"],
                       current=["driveway"], stationary=True, moves=1))
w.tick(5)
w.send("update", **obj("lj", LPR, "car", old, [693, 0, 1204, 140], sub="Sams_Truck", entered=["street", "driveway"],
                       current=["street"], stationary=False, moves=1))
w.run(90)
check("zone flicker without a new position change → no departure",
      not kinds(w) and vehicles_state(w)["vehicles"]["Sams_Truck"]["status"] == "home")

# ---- 9c. the track we announced leaving parks again → state corrected, no second push ----
w = World(known=KNOWN)
old = w.clock() - 3600 * 3
w.send("update", **obj("lk", LPR, "car", old, PARK_LPR, sub="Sams_Truck", entered=["street", "driveway"],
                       current=["driveway"], stationary=True, moves=1))
w.tick(5)
w.send("update", **obj("lk", LPR, "car", old, [693, 0, 1204, 140], sub="Sams_Truck", entered=["street", "driveway"],
                       current=["street"], stationary=False, moves=2))
w.run(90)
w.send("update", **obj("lk", LPR, "car", old, PARK_LPR, sub="Sams_Truck", entered=["street", "driveway"],
                       current=["driveway"], stationary=True, moves=2))
w.run(30)
check("a 'departed' track parked again → home, with no second announcement",
      len(kinds(w)) == 1 and vehicles_state(w)["vehicles"]["Sams_Truck"]["status"] == "home")

# ---- 10. a drive-in that never parks in view (exits the bottom of the frame inside the zone) ----
w = World(known=KNOWN)
t = w.clock() - 3600 * 5
w.send("update", **obj("old", LPR, "car", t, PARK_LPR, sub="Jos_Jeep", entered=["street", "driveway"],
                       current=["street"], moves=3, end=t + 30))   # seeded: not home
t = w.clock()
w.send("new", **obj("j1", LPR, "car", t, [300, 0, 700, 300], sub="Jos_Jeep", entered=["street"], current=["street"], moves=1))
w.tick(5)
w.send("end", **obj("j1", LPR, "car", t, [300, 276, 900, 576], sub="Jos_Jeep", entered=["street", "driveway"],
                    current=["driveway"], moves=3, end=w.clock()))
w.tick(5)
check("ending inside the home zone after driving in is an arrival",
      len(kinds(w)) == 1 and kinds(w)[0]["title"].endswith("arrived home"))

# ---- 11. carriers and strangers are never vehicles ----
w = World(known=KNOWN)
arrive(w, "u1", FD, PARK_FD, sub="ups")
arrive(w, "x1", FD, [100, 0, 300, 200])
w.run(30)
check("a carrier sub_label or a stranger car makes no vehicle push", not kinds(w))

# ---- 12. switched off ----
w = World(opts=smart_alerts.Options(vehicle_alerts=False), known=KNOWN)
arrive(w, "t9", FD, PARK_FD, sub="Sams_Truck")
w.run(30)
check("vehicle_alerts off → nothing", not kinds(w))

finish()
