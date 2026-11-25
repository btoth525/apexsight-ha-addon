"""Lingering (1.31.0): an unknown person or car in an alert zone past the threshold — once per object,
time-sensitive, re-checked against Frigate's own event before it fires.

Run:  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_lingering.py
"""
import os
import sys
import tempfile

os.environ.setdefault("APEX_DATA_DIR", tempfile.mkdtemp(prefix="apexling_"))
sys.path.insert(0, os.path.dirname(__file__))

import smart_alerts  # noqa: E402
from smart_harness import World, check, finish, obj  # noqa: E402

PERSON = [300, 200, 600, 1000]
FD = "Front_Driveway"


def lingers(w):
    return [p for p in w.posts if p.get("kind") == "loitering"]


def stand(w, oid, seconds, camera="doorbell", zones=("side_walk",), sub=None, label="person", box=PERSON,
          entered=None, moves=1):
    t = w.clock()
    ent = list(entered) if entered is not None else list(zones)
    w.events[oid] = {"id": oid, "end_time": None, "sub_label": sub}
    for i in range(int(seconds / 5)):
        w.send("new" if i == 0 else "update",
               **obj(oid, camera, label, t, box, sub=sub, entered=ent, current=list(zones),
                     stationary=i > 3, moves=moves))
        w.tick(5)
    return t


# ---- 1. unknown person at the front door ----
w = World()
stand(w, "p1", 80)
check("not before the 90 s threshold", not lingers(w))
stand_t = w.sa.objs["p1"].start
for _ in range(6):
    w.send("update", **obj("p1", "doorbell", "person", stand_t, PERSON, entered=["side_walk"],
                           current=["side_walk"], stationary=True))
    w.tick(5)
L = lingers(w)
check("one linger push after 90 s", len(L) == 1)
check("…'Someone has been at the front door for …'", L and "Someone has been at the front door for" in L[0]["title"])
check("…time-sensitive", L and L[0]["time_sensitive"] is True)
check("…label loitering first (the app toggle), then the object", L and L[0]["labels"] == ["loitering", "person"])
check("…collapse id per object", L and L[0]["collapse_id"] == "linger-p1")
for _ in range(60):
    w.send("update", **obj("p1", "doorbell", "person", stand_t, PERSON, entered=["side_walk"],
                           current=["side_walk"], stationary=True))
    w.tick(5)
check("once per object, however long they stay", len(lingers(w)) == 1)
check("the fired set is persisted (a restart can't re-fire)", "p1" in (w.store.get("smart:linger_fired") or ""))

# ---- 2. a recognised person is not lingering ----
w = World()
stand(w, "p2", 200, sub="Alex")
check("a person with a sub_label never triggers", not lingers(w))

# ---- 3. face recognised late: Frigate's own event wins at fire time ----
w = World()
w.events["p3"] = {"id": "p3", "end_time": None, "sub_label": "Alex"}
t = w.clock()
for i in range(30):
    w.send("update", **obj("p3", "doorbell", "person", t, PERSON, entered=["side_walk"], current=["side_walk"]))
    w.tick(5)
    w.events["p3"]["sub_label"] = "Alex"
check("re-checked against Frigate before firing (now recognised → no push)", not lingers(w))

# ---- 4. ended in Frigate (missed end message) → no ghost alert ----
w = World()
t = w.clock()
w.events["p4"] = {"id": "p4", "end_time": t + 10, "sub_label": None}
for i in range(30):
    w.send("update", **obj("p4", "doorbell", "person", t, PERSON, entered=["side_walk"], current=["side_walk"]))
    w.tick(5)
    w.events["p4"]["end_time"] = t + 10
check("an event Frigate already ended never fires", not lingers(w))

# ---- 5. walking out of the zone resets the clock (after a grace) ----
w = World()
t = w.clock()
w.events["p5"] = {"id": "p5", "end_time": None, "sub_label": None}
for i in range(14):   # 70 s in
    w.send("update", **obj("p5", "doorbell", "person", t, PERSON, entered=["side_walk"], current=["side_walk"]))
    w.tick(5)
for i in range(6):    # 30 s out
    w.send("update", **obj("p5", "doorbell", "person", t, PERSON, entered=["side_walk"], current=[]))
    w.tick(5)
for i in range(10):   # 50 s back in: 50 s, not 150 s
    w.send("update", **obj("p5", "doorbell", "person", t, PERSON, entered=["side_walk"], current=["side_walk"]))
    w.tick(5)
check("leaving the zone resets the linger clock", not lingers(w))

# ---- 6. outside the alert zones ----
w = World()
stand(w, "p6", 200, camera=FD, zones=("street",))
check("a person in a non-alert zone never triggers", not lingers(w))

# ---- 6b. driveway is cars-only by default; opt people in with :person ----
w = World()
stand(w, "p6b", 200, camera=FD, zones=("driveway",))
check("default: a person in the driveway does not trigger (cars only there)", not lingers(w))
w = World(opts=smart_alerts.Options(linger_zones=smart_alerts.parse_linger_zones("Front_Driveway:driveway:person")))
stand(w, "p6c", 200, camera=FD, zones=("driveway",))
check("Front_Driveway:driveway:person opts people in", len(lingers(w)) == 1)
check("label-qualified entries parse", smart_alerts.parse_linger_zones("A:z:car,A:y,B") == {"A": {"z:car", "y"}, "B": {"*"}})

# ---- 7. unknown car that drove into the driveway and stays 5 min ----
w = World()
stand(w, "c1", 290, camera=FD, zones=("driveway",), label="car", box=[1500, 50, 1800, 250],
      entered=("street", "driveway"))
check("car: not before 5 min", not lingers(w))
stand_t = w.sa.objs["c1"].start
for _ in range(4):
    w.send("update", **obj("c1", FD, "car", stand_t, [1500, 50, 1800, 250], entered=["street", "driveway"],
                           current=["driveway"], stationary=True, moves=1))
    w.tick(5)
L = lingers(w)
check("car: 'An unknown car has been in the driveway for 5 min'",
      len(L) == 1 and "An unknown car has been in the driveway for 5 min" in L[0]["title"])

# ---- 8. a parked household car's re-started track (already inside, never drove in) ----
w = World()
stand(w, "c2", 400, camera=FD, zones=("driveway",), label="car", box=[1500, 50, 1800, 250],
      entered=("driveway",), moves=0)
check("car already parked inside when its track starts never triggers", not lingers(w))

# ---- 9. a known vehicle is never 'unknown' ----
w = World(known=("Sams_Truck",))
stand(w, "c3", 400, camera=FD, zones=("driveway",), label="car", box=[1500, 50, 1800, 250],
      entered=("street", "driveway"), sub="Sams_Truck")
check("a known vehicle never triggers linger", not lingers(w))

# ---- 9b. an unread car pulling into a known vehicle's usual spot (vehicle away) ----
w = World(known=("Sams_Truck",))
w.sa.vehicles["Sams_Truck"].update({"status": "away", "since": w.clock() - 7200, "changed": w.clock() - 7200,
                                     "spots": {FD: [0.4156, 0.0264, 0.1289, 0.3681]}})
stand(w, "c4", 400, camera=FD, zones=("driveway",), label="car", box=[1064, 19, 1394, 284],
      entered=("street", "driveway"))
check("a car parking in an away household vehicle's spot is not an unknown car", not lingers(w))

# ---- 10. camera with no zones ('*'): opt-in (not a default) ----
check("Side_Gate is not watched by default", "Side_Gate" not in smart_alerts.Options().linger_zones)
w = World(opts=smart_alerts.Options(linger_zones=smart_alerts.parse_linger_zones("Side_Gate:*")))
stand(w, "g1", 120, camera="Side_Gate", zones=())
L = lingers(w)
check("Side_Gate:* watches the whole camera", len(L) == 1 and "at the side gate" in L[0]["title"])

# ---- 11. a household member standing with them makes it a visit ----
w = World()
t = w.clock()
w.events["p7"] = {"id": "p7", "end_time": None, "sub_label": None}
for i in range(30):
    w.send("update", **obj("p7", "doorbell", "person", t, PERSON, entered=["side_walk"], current=["side_walk"]))
    w.send("update", **obj("fam", "doorbell", "person", t, PERSON, sub="Alex", entered=["side_walk"],
                           current=["side_walk"]))
    w.tick(5)
check("unknown person with a recognised one present → no linger alert", not lingers(w))

# ---- 11b. one visitor double-tracked by Frigate → one push ----
w = World()
t = w.clock()
for oid in ("d1", "d2"):
    w.events[oid] = {"id": oid, "end_time": None, "sub_label": None}
for i in range(30):
    for oid in ("d1", "d2"):
        w.send("update", **obj(oid, "doorbell", "person", t, PERSON, entered=["side_walk"], current=["side_walk"]))
    w.tick(5)
check("two simultaneous tracks of one visitor → one linger push", len(lingers(w)) == 1)

# ---- 11c. people in the driveway right after a household car came home ----
w = World(known=("Sams_Truck",), opts=smart_alerts.Options(
    linger_zones=smart_alerts.parse_linger_zones("Front_Driveway:driveway")))
w.sa.vehicles["Sams_Truck"].update({"status": "home", "since": w.clock(), "changed": w.clock()})
stand(w, "u1", 200, camera=FD, zones=("driveway",))
check("driveway person right after a known vehicle arrived → no linger", not lingers(w))

# ---- 11d. add-on restart: an hours-old track first seen now doesn't fire at once ----
w = World()
t = w.clock() - 3 * 3600
w.events["r1"] = {"id": "r1", "end_time": None, "sub_label": None}
for i in range(6):
    w.send("update", **obj("r1", "doorbell", "person", t, PERSON, entered=["side_walk"], current=["side_walk"]))
    w.tick(5)
check("a track already 3 h old when first seen counts from now, not its start", not lingers(w))

# ---- 12. options ----
o = smart_alerts.Options.from_env({"LINGER_PERSON_SECONDS": "30", "LINGER_ZONES": "doorbell:porch,Garage",
                                   "SMART_PACKAGE_ALERTS": "false", "LINGER_CAR_SECONDS": "null"})
check("threshold from options", o.linger_person_s == 30)
check("zones parse, bare camera = anywhere", o.linger_zones == {"doorbell": {"porch"}, "Garage": {"*"}})
check("flags parse", o.package_alerts is False and o.vehicle_alerts is True)
check("'null' (unset optional) falls back to the default", o.linger_car_s == 300)
check("out-of-range values clamp", smart_alerts.Options.from_env({"LINGER_PERSON_SECONDS": "1"}).linger_person_s == 15)
w = World(opts=smart_alerts.Options(linger_alerts=False))
stand(w, "p8", 200)
check("linger_alerts off → nothing", not lingers(w))
check("minutes text", smart_alerts.minutes_text(95) == "2 min" and smart_alerts.minutes_text(75) == "75 s")

finish()
