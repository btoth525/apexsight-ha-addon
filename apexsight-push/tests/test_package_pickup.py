"""Package pickup (1.31.0): arm on a resting package, confirm a pickup only with a person present,
after they've left, the spot stayed empty, and the image check agrees.

Run:  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_package_pickup.py
"""
import os
import sys
import tempfile

os.environ.setdefault("APEX_DATA_DIR", tempfile.mkdtemp(prefix="apexpkg_"))
sys.path.insert(0, os.path.dirname(__file__))

import smart_alerts  # noqa: E402
from smart_harness import World, check, finish, obj  # noqa: E402

BOX = [530, 727, 768, 1024]          # resting box, lower right of the doorbell frame
BOX_NUDGED = [535, 730, 768, 1024]   # same spot, re-detected a few pixels off
PERSON = [300, 200, 600, 1000]


def delivered(w, pid="pkg1"):
    """A package placed at t0 and seen resting for a minute (armed)."""
    t0 = w.clock()
    w.send("new", **obj(pid, "doorbell", "package", t0, BOX, entered=["side_walk"], current=["side_walk"]))
    for _ in range(12):
        w.tick(5)
        w.send("update", **obj(pid, "doorbell", "package", t0, BOX, entered=["side_walk"],
                               current=["side_walk"], stationary=True))
    return t0


def pickup(w, pid="pkg1", person="per1", sub=None, leave_after=10):
    """A person walks up, the package track ends while they're there, they leave."""
    t = w.clock()
    w.send("new", **obj(person, "doorbell", "person", t, PERSON, sub=sub, entered=["side_walk"],
                        current=["side_walk"]))
    w.tick(5)
    pkg_start = w.sa.objs[pid].start
    w.send("end", **obj(pid, "doorbell", "package", pkg_start, BOX, entered=["side_walk"],
                        current=["side_walk"], stationary=True, end=w.clock()))
    w.tick(leave_after)
    w.send("end", **obj(person, "doorbell", "person", t, PERSON, sub=sub, entered=["side_walk"],
                        current=[], end=w.clock()))
    w.events[person] = {"id": person, "sub_label": sub, "end_time": w.clock()}
    w.reviews[person] = "rev-" + person


# ---- 1. unknown person takes the package ----
w = World(ncc=0.1)
delivered(w)
check("a resting package arms the watch", any(s.armed for s in w.sa.spots))
pickup(w)
w.run(60)
check("no push before the confirm window has passed", not w.posts)
w.run(90)
check("one pickup push after the confirm window", len(w.posts) == 1)
p = w.posts[0] if w.posts else {}
check("unknown → 'Someone took the package'", "Someone took the package" in p.get("title", ""))
check("unknown pickup is time-sensitive", p.get("time_sensitive") is True)
check("body reads 'Package picked up · Doorbell · <time>'",
      p.get("body", "").startswith("Package picked up · Doorbell · ") and p.get("body", "").endswith("M"))
check("carries the synthetic label so the app's toggle can mute it", p.get("labels") == ["package_pickup"])
check("kind is set (relay skips render/dedup)", p.get("kind") == "package_pickup")
check("media: preview GIF around the moment + the person's snapshot",
      "/api/doorbell/start/" in p.get("snapshot_url", "") and "/api/events/per1/snapshot.jpg" in p.get("thumbnail_url", ""))
check("deep-links to the person's review", p.get("review_id") == "rev-per1" and p.get("apex_url") == "apex://review?id=rev-per1")
check("unique collapse id (never replaces the delivery alert)", p.get("collapse_id") == "pickup-per1")
w.run(600)
check("fires once", len(w.posts) == 1)

# ---- 2. a recognised person (face recognised only after the track ended) ----
w = World(ncc=0.1)
delivered(w)
pickup(w, sub=None)
w.events["per1"]["sub_label"] = "Alex"
w.run(150)
p = w.posts[0] if w.posts else {}
check("known person named from Frigate's refreshed event", "Alex picked up the package" in p.get("title", ""))
check("known pickup is not time-sensitive", p.get("time_sensitive") is False)

# ---- 3. flicker with nobody there ----
w = World(ncc=0.1)
t0 = delivered(w)
w.send("end", **obj("pkg1", "doorbell", "package", t0, BOX, current=["side_walk"], stationary=True,
                    end=w.clock()))
w.run(900)
check("a package track ending with no person present never alerts", not w.posts)
check("…and the watch stays armed", any(s.armed for s in w.sa.spots))

# ---- 4. occluded, then re-detected at the same spot ----
w = World(ncc=0.1)
delivered(w)
pickup(w)
w.tick(30)
w.send("new", **obj("pkg2", "doorbell", "package", w.clock(), BOX_NUDGED, current=["side_walk"]))
w.run(600)
check("re-detected at the same spot cancels the pickup", not w.posts)

# ---- 5. the image check still sees the package ----
w = World(ncc=0.9)
delivered(w)
pickup(w)
w.run(300)
check("image check consulted", w.ncc_calls >= 1)
check("package still in frame → no push", not w.posts)
check("…and the spot stays watched", any(s.armed for s in w.sa.spots))

# ---- 5b. the image check compares against the NEWEST resting sighting, not the delivery ----
refs = []
w = World(ncc=0.1)
w.sa._region_check = lambda camera, ref, box: refs.append(ref) or 0.1
delivered(w)
t = w.clock()
w.send("end", **obj("pkg1", "doorbell", "package", w.sa.objs["pkg1"].start, BOX, current=["side_walk"],
                    stationary=True, end=t))
w.tick(600)   # hours later the same box is re-detected (lighting changed) and rests a while
t2 = w.clock()
w.send("new", **obj("pkg9", "doorbell", "package", t2, BOX_NUDGED, current=["side_walk"]))
for _ in range(3):
    w.tick(5)
    w.send("update", **obj("pkg9", "doorbell", "package", t2, BOX_NUDGED, current=["side_walk"], stationary=True))
pickup(w, pid="pkg9")
w.run(150)
check("the image check's reference is the newest resting sighting", refs and refs[0] == "pkg9")

# ---- 6. a box carried through (2 s track) is not a delivery ----
w = World(ncc=0.1)
t = w.clock()
w.send("new", **obj("per9", "doorbell", "person", t, PERSON, current=["side_walk"]))
w.send("new", **obj("pkgc", "doorbell", "package", t, BOX, current=["side_walk"]))
w.tick(2)
w.send("end", **obj("pkgc", "doorbell", "package", t, BOX, current=["side_walk"], end=w.clock()))
w.tick(5)
w.send("end", **obj("per9", "doorbell", "person", t, PERSON, current=[], end=w.clock()))
w.run(600)
check("a carried package (short track) never arms", not w.posts and not any(s.armed for s in w.sa.spots))

# ---- 7. someone still standing there holds the confirmation ----
w = World(ncc=0.1)
delivered(w)
t = w.clock()
w.send("new", **obj("per1", "doorbell", "person", t, PERSON, current=["side_walk"]))
w.tick(5)
w.send("end", **obj("pkg1", "doorbell", "package", w.sa.objs["pkg1"].start, BOX, current=["side_walk"],
                    end=w.clock()))
for _ in range(40):   # 200 s with the person still there, sending updates
    w.tick(5)
    w.send("update", **obj("per1", "doorbell", "person", t, PERSON, current=["side_walk"]))
pickups = lambda: [x for x in w.posts if x.get("kind") == "package_pickup"]
check("no pickup while the person is still at the door", not pickups())
check("(the person standing there 3 min IS a linger alert)", any(x.get("kind") == "loitering" for x in w.posts))
w.send("end", **obj("per1", "doorbell", "person", t, PERSON, current=[], end=w.clock()))
w.run(150)
check("pickup once they leave and the spot stays empty", len(pickups()) == 1)

# ---- 8. image check unavailable → the other guards decide (fail-open) ----
w = World(ncc=None)
delivered(w)
pickup(w)
w.run(150)
check("no image check available still announces the pickup", len(w.posts) == 1)

# ---- 9. a carrier visit lowers the resting time ----
w = World(ncc=0.1)
w.send("new", **obj("van", "Front_Driveway", "car", w.clock(), [100, 0, 300, 200], current=["street"],
                    attrs=["ups"]))
t = w.clock()
w.send("new", **obj("pkg1", "doorbell", "package", t, BOX, current=["side_walk"]))
w.tick(5)
for _ in range(3):
    w.send("update", **obj("pkg1", "doorbell", "package", t, BOX, current=["side_walk"], stationary=True))
    w.tick(5)
w.send("update", **obj("pkg1", "doorbell", "package", t, BOX, current=["side_walk"], stationary=True))
check("after a carrier visit a ~15 s rest arms the watch", any(s.armed for s in w.sa.spots))

# ---- 9b. the delivery itself: a box carried up the walk for 20 s, never stationary ----
w = World(ncc=0.1)
w.send("new", **obj("van", "Front_Driveway", "car", w.clock(), [100, 0, 300, 200], current=["street"], attrs=["ups"]))
t = w.clock()
w.send("new", **obj("drv", "doorbell", "person", t, PERSON, current=["side_walk"]))
w.send("new", **obj("pkgd", "doorbell", "package", t, BOX, current=["side_walk"]))
for _ in range(4):
    w.tick(5)
    w.send("update", **obj("pkgd", "doorbell", "package", t, BOX, current=["side_walk"], stationary=False))
w.send("end", **obj("pkgd", "doorbell", "package", t, BOX, current=["side_walk"], end=w.clock()))
w.send("end", **obj("drv", "doorbell", "person", t, PERSON, current=[], end=w.clock()))
w.run(600)
check("a box still being carried (moving, 20 s) never arms — the drop-off isn't a pickup",
      not [p for p in w.posts if p.get("kind") == "package_pickup"])

# ---- 10. switched off ----
w = World(opts=smart_alerts.Options(package_alerts=False), ncc=0.1)
delivered(w)
pickup(w)
w.run(300)
check("package_alerts off → nothing", not w.posts)

# ---- helpers ----
check("NCC: identical patches correlate", smart_alerts.ncc(bytes(range(256)), bytes(range(256))) > 0.99)
check("NCC: inverted patch anti-correlates",
      smart_alerts.ncc(bytes(range(256)), bytes(255 - v for v in range(256))) < -0.99)
check("NCC: size mismatch is unknown, not 'gone'", smart_alerts.ncc(b"\x00" * 4, b"\x00" * 9) is None)
check("same_spot: a few pixels off is the same spot",
      smart_alerts.same_spot([0.69, 0.71, 0.31, 0.29], [0.70, 0.72, 0.30, 0.28]))
check("same_spot: across the frame is not", not smart_alerts.same_spot([0.1, 0.1, 0.1, 0.1], [0.7, 0.7, 0.2, 0.2]))
check("clock text has no leading zero", smart_alerts.clock_text(1_800_000_000).split(":")[0].isdigit()
      and not smart_alerts.clock_text(1_800_000_000).startswith("0"))

finish()
