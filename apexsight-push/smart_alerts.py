"""Smart alerts (1.31.0): package pickup, vehicle home/away, and lingering.

The bridge feeds every message from Frigate's per-object `frigate/events` topic into `on_event`
(cheap, in memory, on paho's network thread) and calls `tick` every few seconds from a worker thread
(that is where every Frigate read, image check and relay POST happens, so the MQTT loop is never
blocked). Everything that talks to the outside world is injected, so the tests drive the exact same
code with a fake clock and fake Frigate.

Three watchers, each built from what Frigate's events actually look like on a real install — the
comments carry the measurements that shaped them:

* Package pickup. A package only counts once it has been seen resting (`package_min_dwell_s`): the
  2-3 s package tracks Frigate makes while a person CARRIES a box are not deliveries. When the
  package's track ends, a pickup is only considered if a person was on the same camera at that
  moment (a box cannot leave on its own; without a person the end is detector flicker, which on one
  real afternoon produced ~15 package tracks for one box that never moved). The pickup is then
  confirmed only after everyone has left, the spot has stayed empty for `package_confirm_s` with no
  new package track there, and — the strong guard — the package's region in the camera's current
  frame no longer looks like the package (Frigate only re-detects a still box when something moves
  near it, so "no new detection" alone missed a box that sat untouched for 11 minutes).
* Vehicles. Known vehicles are the KEYS of Frigate's `lpr.known_plates` (the sub_label LPR assigns),
  never the plate text (one truck read as four different plates in a week). Plates were read on
  about a third of driveway movements, so identity is carried across the two driveway cameras by
  time and forward to a re-started track parked in the same spot. Arrival = an identified track
  that came in from outside the home zones and then parked (stationary) in them or ended inside
  them; departure = an identified track that was home and left the home zones, confirmed for
  `vehicle_depart_confirm_s` (cancelled if the vehicle is still seen parked).
* Lingering. An unknown person (no sub_label) or an unknown car that stays in an alert zone past the
  threshold. Once per object, time-sensitive, re-checked against Frigate's own event right before
  it fires (still open, still unknown).

Every push goes through the relay's /v1/notify with a `kind`, so the household gate, per-device
mutes, quiet hours and triggers all apply. The label each one carries is synthetic
(`package_pickup`, `vehicle_presence`, `loitering`), so the app's per-object toggles switch each kind
off without touching the "person"/"car" mutes.
"""
from __future__ import annotations

import datetime
import json
import math
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

LABEL_PICKUP = "package_pickup"
LABEL_VEHICLE = "vehicle_presence"
LABEL_LINGER = "loitering"

CARRIERS = {"amazon", "ups", "fedex", "usps", "dhl", "an_post", "purolator", "postnl", "nzpost",
            "postnord", "gls", "dpd", "royal_mail", "canada_post"}
VEHICLE_LABELS = {"car", "truck", "motorcycle", "bus"}

# How long an ended object stays in memory (pickup identity and cross-camera vehicle identity need
# recent history; nothing needs more than this).
_KEEP_ENDED_S = 20 * 60
# A pickup candidate needs a person on the camera within this many seconds of the package track's end.
_PERSON_OVERLAP_S = 20
# How long a pickup waits for people who are STILL there before it gives up waiting and checks anyway.
_PERSON_WAIT_CAP_S = 10 * 60
# Two package boxes are "the same spot" when their centres are this close (fraction of the frame).
_SPOT_CENTRE = 0.10
# NCC of the package region (then vs now) at or above which the package is judged still there.
# Measured on real doorbell frames: same box 0.95-1.00 (0.50-0.57 with a mat dropped on top of it),
# box gone 0.006-0.36 (0.36 with the person still at the edge of the crop).
_STILL_THERE_NCC = 0.45
# A package Frigate never flags stationary still arms after resting this long in one track.
_UNFLAGGED_DWELL_S = 120.0
# Track first seen this long after it started (an add-on restart mid-track) → state only, no push.
_STALE_TRACK_S = 10 * 60
# Cross-camera identity: the two driveway cameras start tracking the same car within seconds.
_CROSS_CAM_S = 90
# A re-started track parked in a known vehicle's spot.
_SPOT_IOU = 0.3
# A carrier seen within this window lowers the arming dwell for a package (a delivery just happened).
_CARRIER_WINDOW_S = 15 * 60
_CARRIER_DWELL_S = 10.0
# Linger: briefly out of the zone (Frigate's zone inertia, a step back) doesn't reset the clock.
_LINGER_OUT_GRACE_S = 15.0
# No update for this long → re-read the event from Frigate before trusting it.
_STALE_UPDATE_S = 90.0
_FIRED_TTL_S = 24 * 3600
# One linger push per camera and object class in this window: Frigate often double-tracks one person
# (two tracks, same moment — measured twice in a week on the driveway), and that is one visitor.
_LINGER_CAMERA_COOLDOWN_S = 10 * 60
# People on the vehicle cameras right after a known vehicle came or went are the household unloading
# or leaving (a week of replay: every driveway-person linger near an arrival was this).
_HOUSEHOLD_ACTIVITY_S = 20 * 60

PLACE_PHRASE = {"doorbell": "at the front door", "Front_Driveway": "in the driveway",
                "Front_Driveway_LPR": "in the driveway", "Side_Gate": "at the side gate",
                "Backyard_Wide": "in the backyard", "Garage": "in the garage"}


def clock_text(ts: float) -> str:
    """"4:12 PM" in the add-on's local time. Manual, not "%-I": musl's strftime has no "-" flag."""
    dt = datetime.datetime.fromtimestamp(ts)
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def titleize(s: str) -> str:
    return s.replace("_", " ").title() if s else s


def display_name(sub_label: str) -> str:
    """"Sams_Truck" → "Sam's Truck", "Alex" → "Alex"."""
    words = [w for w in (sub_label or "").replace("-", "_").split("_") if w]
    if not words:
        return sub_label or ""
    words = [w[:1].upper() + w[1:] for w in words]
    first = words[0]
    if len(words) >= 2 and len(first) > 2 and first.endswith("s") and not first.endswith("ss"):
        words[0] = first[:-1] + "'s"
    return " ".join(words)


def vehicle_emoji(name: str) -> str:
    n = (name or "").lower()
    if "truck" in n or "pickup" in n:
        return "\U0001f6fb"   # pickup truck
    if any(k in n for k in ("jeep", "suv", "van", "4runner", "bronco")):
        return "\U0001f699"   # SUV
    return "\U0001f697"       # car


def minutes_text(seconds: float) -> str:
    if seconds < 90:
        return f"{int(round(seconds))} s"
    return f"{max(1, int(round(seconds / 60.0)))} min"


def _sub_label(value) -> Optional[str]:
    """MQTT carries ["Name", score]; the HTTP API carries "Name"."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def iou(a, b) -> float:
    """Intersection-over-union of two normalized [x, y, w, h] boxes."""
    if not a or not b:
        return 0.0
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def same_spot(a, b) -> bool:
    if not a or not b:
        return False
    ca = (a[0] + a[2] / 2, a[1] + a[3] / 2)
    cb = (b[0] + b[2] / 2, b[1] + b[3] / 2)
    return (abs(ca[0] - cb[0]) <= _SPOT_CENTRE and abs(ca[1] - cb[1]) <= _SPOT_CENTRE) or iou(a, b) >= 0.25


def ncc(a: bytes, b: bytes) -> Optional[float]:
    """Normalized cross-correlation of two equal-size grayscale patches (lighting-tolerant)."""
    if not a or not b or len(a) != len(b):
        return None
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    da = [v - ma for v in a]
    db = [v - mb for v in b]
    na = math.sqrt(sum(v * v for v in da))
    nb = math.sqrt(sum(v * v for v in db))
    if na < 1e-6 or nb < 1e-6:
        # A flat patch: equal means "the same flat thing", anything else is a change.
        return 1.0 if abs(ma - mb) < 8 else 0.0
    return sum(x * y for x, y in zip(da, db)) / (na * nb)


def gray_patch(jpeg: bytes, box, size: int = 16, margin: float = 0.15) -> Optional[bytes]:
    """`box` (normalized xywh, grown by `margin`) of a JPEG as size×size gray bytes, via ffmpeg (in
    the add-on image already; Pillow is not). None on any failure."""
    if not jpeg or not box:
        return None
    x, y, w, h = box
    x0, y0 = max(0.0, x - w * margin), max(0.0, y - h * margin)
    x1, y1 = min(1.0, x + w * (1 + margin)), min(1.0, y + h * (1 + margin))
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    vf = (f"crop=iw*{x1 - x0:.4f}:ih*{y1 - y0:.4f}:iw*{x0:.4f}:ih*{y0:.4f},"
          f"scale={size}:{size}:flags=area,format=gray")
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-vf", vf, "-frames:v", "1",
                              "-f", "rawvideo", "pipe:1"],
                             input=jpeg, capture_output=True, timeout=15).stdout
    except Exception:  # noqa: BLE001 — no ffmpeg / bad image: the check is optional
        return None
    return out if len(out) == size * size else None


@dataclass
class Options:
    package_alerts: bool = True
    package_confirm_s: float = 120.0
    package_min_dwell_s: float = 30.0
    package_image_check: bool = True
    vehicle_alerts: bool = True
    vehicle_cameras: tuple = ("Front_Driveway", "Front_Driveway_LPR")
    vehicle_home_zones: tuple = ("driveway",)
    vehicle_depart_confirm_s: float = 60.0
    linger_alerts: bool = True
    linger_person_s: float = 90.0
    linger_car_s: float = 300.0
    # camera -> set of zones ("*" = anywhere on that camera, for a camera with no zones).
    linger_zones: dict = field(default_factory=lambda: parse_linger_zones(DEFAULT_LINGER_ZONES))

    @classmethod
    def from_env(cls, env) -> "Options":
        def flag(key, default):
            v = (env.get(key) or "").strip().lower()
            return default if v in ("", "null") else v in ("1", "true", "yes", "on")

        def num(key, default, lo, hi):
            try:
                v = float((env.get(key) or "").strip())
            except ValueError:
                return default
            return min(max(v, lo), hi)

        def csv(key, default):
            v = (env.get(key) or "").strip()
            items = tuple(s.strip() for s in v.split(",") if s.strip()) if v and v != "null" else ()
            return items or default

        d = cls()
        zones_raw = (env.get("LINGER_ZONES") or "").strip()
        return cls(
            package_alerts=flag("SMART_PACKAGE_ALERTS", d.package_alerts),
            package_confirm_s=num("PACKAGE_CONFIRM_SECONDS", d.package_confirm_s, 60, 3600),
            package_min_dwell_s=num("PACKAGE_MIN_DWELL_SECONDS", d.package_min_dwell_s, 5, 600),
            package_image_check=flag("PACKAGE_IMAGE_CHECK", d.package_image_check),
            vehicle_alerts=flag("SMART_VEHICLE_ALERTS", d.vehicle_alerts),
            vehicle_cameras=csv("VEHICLE_CAMERAS", d.vehicle_cameras),
            vehicle_home_zones=csv("VEHICLE_HOME_ZONES", d.vehicle_home_zones),
            vehicle_depart_confirm_s=num("VEHICLE_DEPART_CONFIRM_SECONDS", d.vehicle_depart_confirm_s, 0, 1800),
            linger_alerts=flag("SMART_LINGER_ALERTS", d.linger_alerts),
            linger_person_s=num("LINGER_PERSON_SECONDS", d.linger_person_s, 15, 3600),
            linger_car_s=num("LINGER_CAR_SECONDS", d.linger_car_s, 30, 7200),
            linger_zones=(parse_linger_zones(zones_raw) if zones_raw and zones_raw != "null"
                          else d.linger_zones),
        )


# Side_Gate is deliberately NOT a default: it has no zones and no face recognition, and a week of its
# 2-minute "person" tracks were the family dog and a garden cart (11 false alerts in one afternoon).
# Add "Side_Gate:*" to the option to watch it anyway.
# The driveway is cars-only by default: face recognition matched 3 of 892 driveway person tracks in a
# week, so every resident washing a car or unloading groceries read as "someone" (11 alerts in the
# replayed week, all household). Add "Front_Driveway:driveway:person" to watch people there too.
DEFAULT_LINGER_ZONES = "doorbell:side_walk,doorbell:porch,Front_Driveway:driveway:car"


def parse_linger_zones(raw: str) -> dict:
    """"doorbell:side_walk,Front_Driveway:driveway:car,Side_Gate" →
    {"doorbell": {"side_walk"}, "Front_Driveway": {"driveway:car"}, "Side_Gate": {"*"}}.
    A bare camera (or zone "*") means anywhere on it; an optional third part limits the entry to
    "person" or "car" (car covers every vehicle label)."""
    out: dict = {}
    for item in (raw or "").split(","):
        parts = [x.strip() for x in item.strip().split(":")]
        if not parts or not parts[0]:
            continue
        zone = parts[1] if len(parts) > 1 and parts[1] else "*"
        label = parts[2].lower() if len(parts) > 2 and parts[2] else ""
        out.setdefault(parts[0], set()).add(f"{zone}:{label}" if label in ("person", "car") else zone)
    return out


def linger_zones_for(table: dict, camera: str, label: str) -> set:
    """The zones of `camera` that watch objects of `label` ("*" = anywhere)."""
    kind = "person" if label == "person" else "car"
    out = set()
    for entry in table.get(camera) or ():
        zone, _, only = entry.partition(":")
        if not only or only == kind:
            out.add(zone)
    return out


@dataclass
class Obj:
    id: str
    camera: str
    label: str
    start: float
    first_seen: float
    end: Optional[float] = None
    sub_label: Optional[str] = None
    entered: list = field(default_factory=list)
    current: list = field(default_factory=list)
    stationary: bool = False
    position_changes: int = 0
    box: Optional[list] = None          # normalized [x, y, w, h]
    raw_box: Optional[list] = None      # as received (MQTT: pixel x1,y1,x2,y2)
    last_seen: float = 0.0
    attrs: set = field(default_factory=set)
    seen_home: bool = False             # vehicles: was inside the home zones at some point
    stationary_home_at: float = 0.0     # vehicles: last time seen parked in the home zones
    parked_changes: int = -1            # vehicles: Frigate's position_changes while it sat parked
    zone_since: Optional[float] = None  # linger
    zone_out_since: Optional[float] = None
    stationary_since: Optional[float] = None
    household: bool = False             # linger: seen in a household vehicle's spot — never "unknown"


@dataclass
class Spot:
    """Where a package rests on one camera."""
    camera: str
    box: list
    created: float
    dwell: float = 0.0                  # longest single resting track, seconds
    armed: bool = False
    last_event: str = ""                # the longest-resting track here (the spot's geometry)
    # The NEWEST track that rested in the same place: the image check's reference. The delivery's
    # own snapshot is often hours old — a daytime frame against a night IR frame scored 0.26 with the
    # box still there (measured), which would announce a time-sensitive "Someone took the package".
    ref_event: str = ""
    tracks: set = field(default_factory=set)
    last_activity: float = 0.0
    pending: Optional[dict] = None      # {"ended": ts, "persons": [ids], "since": ts}


class SmartAlerts:
    def __init__(self, opts: Options, *, post: Callable[[dict], object],
                 frigate_json: Callable[[str], object], frigate_bytes: Callable[[str], Optional[bytes]],
                 store_get: Callable[[str], Optional[str]], store_set: Callable[[str, str], None],
                 public_base: str, pairing_code: str, clock: Callable[[], float] = time.time,
                 log: Callable[..., None] = print,
                 region_check: Optional[Callable[[str, str, list], Optional[float]]] = None):
        self.opts = opts
        self._post = post
        self._fj = frigate_json
        self._fb = frigate_bytes
        self._get = store_get
        self._set = store_set
        self.public = (public_base or "").rstrip("/")
        self.code = pairing_code
        self.clock = clock
        self.log = log
        self._region_check = region_check or self._default_region_check
        self._lock = threading.RLock()
        self.objs: dict[str, Obj] = {}
        self.spots: list[Spot] = []
        # Last carrier sighting on ANY camera: the van is on the driveway camera, the box at the door.
        self.carrier_seen_at = -1e18
        self.dims: dict[str, tuple] = {}             # camera -> (detect width, height)
        self.known: set[str] = set()                 # known vehicle sub_labels
        self._config_loaded_at = 0.0
        self.adopted: dict[str, str] = {}            # track id -> vehicle sub_label
        self.vehicles: dict[str, dict] = self._load_json(self._vehicle_key(), {}).get("vehicles", {})
        self.depart_pending: dict[str, dict] = {}    # vehicle -> {"track", "at"}
        self._outbox: list[tuple[str, dict]] = []    # (kind, facts) decided under the lock, sent in tick
        self.fired_linger: dict[str, float] = self._load_json("smart:linger_fired", {})
        self.linger_camera_at: dict[str, float] = {}  # "camera|label" -> last linger push

    # ---- persistence ----------------------------------------------------------------------------

    def _vehicle_key(self) -> str:
        return f"vehicles:{self.code}"

    def _load_json(self, key, default):
        try:
            raw = self._get(key)
            val = json.loads(raw) if raw else default
            return val if isinstance(val, type(default)) else default
        except Exception:  # noqa: BLE001
            return default

    def _save_vehicles(self) -> None:
        self._set(self._vehicle_key(), json.dumps({
            "updated": self.clock(),
            "known": sorted(self.known),
            "vehicles": self.vehicles,
        }))

    # ---- Frigate config (known vehicles + detect sizes) -------------------------------------------

    def refresh_config(self, force: bool = False) -> None:
        now = self.clock()
        if not force and self._config_loaded_at and now - self._config_loaded_at < 3600:
            return
        cfg = self._fj("/api/config")
        if not isinstance(cfg, dict):
            # Retry in a minute rather than every tick.
            self._config_loaded_at = now - 3600 + 60
            return
        with self._lock:
            for cam, cc in (cfg.get("cameras") or {}).items():
                det = (cc or {}).get("detect") or {}
                if det.get("width") and det.get("height"):
                    self.dims[cam] = (float(det["width"]), float(det["height"]))
            plates = ((cfg.get("lpr") or {}).get("known_plates") or {})
            if isinstance(plates, dict):
                self.known = {k for k in plates if k and k.lower() not in CARRIERS}
            for name in self.known:
                self.vehicles.setdefault(name, {"name": display_name(name), "status": "unknown",
                                                "since": 0, "changed": 0})
            for o in self.objs.values():
                if o.box is None and o.raw_box is not None:
                    o.box = self._norm(o.camera, o.raw_box)
            self._config_loaded_at = now
        self._save_vehicles()

    def _norm(self, camera: str, raw) -> Optional[list]:
        """MQTT boxes are detect-resolution pixels [x1, y1, x2, y2]; normalize to [x, y, w, h]."""
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            return None
        try:
            vals = [float(v) for v in raw]
        except (TypeError, ValueError):
            return None
        dims = self.dims.get(camera)
        if not dims:
            return None
        w, h = dims
        return [vals[0] / w, vals[1] / h, (vals[2] - vals[0]) / w, (vals[3] - vals[1]) / h]

    # ---- event intake (paho thread: memory only, no I/O) ----------------------------------------

    def on_event(self, msg_type: str, after: dict) -> None:
        if not isinstance(after, dict) or not after.get("id"):
            return
        now = self.clock()
        with self._lock:
            o = self._upsert(after, msg_type, now)
            if o is None:
                return
            label = o.label
            if label in CARRIERS or o.sub_label in CARRIERS or (o.attrs & CARRIERS):
                self.carrier_seen_at = now
            if self.opts.package_alerts:
                if label == "package":
                    self._package_event(o, msg_type, now)
            if self.opts.vehicle_alerts and label in VEHICLE_LABELS:
                self._vehicle_event(o, msg_type, now)
            if self.opts.linger_alerts:
                self._linger_event(o, now)

    def _upsert(self, a: dict, msg_type: str, now: float) -> Optional[Obj]:
        oid = str(a["id"])
        o = self.objs.get(oid)
        try:
            start = float(a.get("start_time") or now)
        except (TypeError, ValueError):
            start = now
        if o is None:
            o = Obj(id=oid, camera=str(a.get("camera") or ""), label=str(a.get("label") or ""),
                    start=start, first_seen=now)
            self.objs[oid] = o
        sub = _sub_label(a.get("sub_label"))
        if sub:
            o.sub_label = sub
        o.entered = list(a.get("entered_zones") or o.entered)
        o.current = list(a.get("current_zones") or [])
        o.stationary = bool(a.get("stationary"))
        try:
            o.position_changes = int(a.get("position_changes") or 0)
        except (TypeError, ValueError):
            pass
        if a.get("box") is not None:
            o.raw_box = a.get("box")
            o.box = self._norm(o.camera, o.raw_box) or o.box
        o.attrs = {str(x.get("label")) for x in (a.get("current_attributes") or [])
                   if isinstance(x, dict) and x.get("label")} | set((a.get("attributes") or {}).keys())
        o.last_seen = now
        end = a.get("end_time")
        if msg_type == "end" or end:
            try:
                o.end = float(end) if end else now
            except (TypeError, ValueError):
                o.end = now
        return o

    # ---- package pickup -------------------------------------------------------------------------

    def _spot_for(self, o: Obj) -> Optional[Spot]:
        # A track keeps its spot: its box moves while it's carried in and set down, and its end
        # message must land on the spot it armed.
        for s in self.spots:
            if o.id in s.tracks:
                return s
        if o.box is None:
            return None
        for s in self.spots:
            if s.camera == o.camera and same_spot(s.box, o.box):
                return s
        return None

    def _package_event(self, o: Obj, msg_type: str, now: float) -> None:
        spot = self._spot_for(o)
        if spot is None:
            if o.box is None or o.end is not None and (o.end - o.start) < 1:
                return
            spot = Spot(camera=o.camera, box=list(o.box), created=now)
            self.spots.append(spot)
        if o.id not in spot.tracks and spot.pending and o.start > spot.pending["ended"] - 1:
            # The package is back at the same spot — it was occluded or the detector flickered.
            self.log(f"[smart] package at {o.camera} re-detected; pickup cancelled")
            spot.pending = None
        spot.tracks.add(o.id)
        if not spot.last_event:
            spot.last_event = o.id
        spot.last_activity = now
        # Resting time = how long Frigate has called it stationary, NOT the track's age: a driver
        # carrying the box up the walk is a 10-20 s package track that is moving the whole time
        # (measured: one such track armed on age alone and turned the delivery into a "pickup").
        if o.stationary and o.end is None:
            if o.stationary_since is None:
                o.stationary_since = now
        elif o.end is None:
            o.stationary_since = None
        rest = ((o.end or now) - o.stationary_since) if o.stationary_since is not None else 0.0
        age = (o.end or now) - o.start
        # The spot keeps the box of its longest-RESTING track: a 2 s mis-detection or a box being
        # carried must not drag it (measured: one did, and the real pickup hours later was missed).
        if o.box and rest >= spot.dwell:
            spot.box = list(o.box)
            spot.last_event = o.id
        if o.box and rest >= 3 and iou(o.box, spot.box) >= 0.5:
            spot.ref_event = o.id
        spot.dwell = max(spot.dwell, rest)
        need = self.opts.package_min_dwell_s
        if now - self.carrier_seen_at <= _CARRIER_WINDOW_S:
            need = min(need, _CARRIER_DWELL_S)
        if not spot.armed and (rest >= need or age >= max(need, _UNFLAGGED_DWELL_S)):
            spot.armed = True
            self.log(f"[smart] package watch armed on {o.camera} (rested {int(spot.dwell)} s)")
        if o.end is None:
            return
        spot.tracks.discard(o.id)
        if not spot.armed or spot.tracks:
            return
        persons = [p.id for p in self._persons_near(o.camera, o.end)]
        if not persons:
            # No one at the door: the box can't leave by itself. Detector flicker — keep watching.
            return
        spot.pending = {"ended": o.end, "persons": persons, "since": now, "package": o.id}
        self.log(f"[smart] package on {o.camera} gone with {len(persons)} person(s) present — confirming")

    def _persons_near(self, camera: str, t: float) -> list:
        out = []
        for p in self.objs.values():
            if p.camera != camera or p.label != "person":
                continue
            p_end = p.end if p.end is not None else p.last_seen
            if p.start <= t + _PERSON_OVERLAP_S and p_end >= t - _PERSON_OVERLAP_S:
                out.append(p)
        return out

    def _package_due(self, now: float) -> list:
        """Pending pickups whose wait is over (under the lock). Returns spots to verify."""
        due = []
        for s in self.spots:
            p = s.pending
            if not p:
                continue
            persons = [self.objs[i] for i in p["persons"] if i in self.objs]
            # Anyone still on this camera holds the confirmation (they may be standing in front of it).
            active = [x for x in self.objs.values() if x.camera == s.camera and x.label == "person"
                      and x.end is None and now - x.last_seen < 30]
            if active and now - p["since"] < _PERSON_WAIT_CAP_S:
                continue
            left = max([p["ended"]] + [(x.end if x.end is not None else x.last_seen) for x in persons])
            if now - left < self.opts.package_confirm_s:
                continue
            due.append(s)
        return due

    def _confirm_pickup(self, s: Spot) -> None:
        """I/O half of a pickup: image check, identity, media. Called from tick, outside the lock."""
        with self._lock:
            p = s.pending
            if not p:
                return
            camera, box, ref = s.camera, list(s.box), (s.ref_event or s.last_event)
            persons = [self.objs[i] for i in p["persons"] if i in self.objs]
            ended = p["ended"]
        if self.opts.package_image_check:
            score = self._region_check(camera, ref, box)
            if score is not None and score >= _STILL_THERE_NCC:
                self.log(f"[smart] package on {camera} still in frame (ncc {score:.2f}); no pickup")
                with self._lock:
                    s.pending = None
                return
        names = []
        for person in persons:
            name = person.sub_label
            ev = self._fj(f"/api/events/{person.id}")
            if isinstance(ev, dict):
                name = _sub_label(ev.get("sub_label")) or name
            if name and name.lower() not in CARRIERS and name not in self.known and name not in names:
                names.append(name)
        lead = next((x for x in persons if x.sub_label in names), persons[0] if persons else None)
        review_id = self._review_for(lead.id) if lead else ""
        if not names and review_id:
            rv = self._fj(f"/api/review/{review_id}")
            subs = ((rv or {}).get("data") or {}).get("sub_labels") if isinstance(rv, dict) else None
            for n in subs or []:
                if n and n.lower() not in CARRIERS and n not in self.known and n not in names:
                    names.append(n)
        with self._lock:
            if s.pending is None:   # re-detected while we were checking
                return
            if s in self.spots:
                self.spots.remove(s)
        self._send_pickup(camera, ended, names, lead.id if lead else ref, review_id)

    def _send_pickup(self, camera: str, ended: float, names: list, event_id: str, review_id: str) -> None:
        if names:
            who = " and ".join(display_name(n) for n in names[:2])
            title = f"\U0001f4e6 {who} picked up the package"
        else:
            title = "\U0001f4e6 Someone took the package"
        body = f"Package picked up · {titleize(camera)} · {clock_text(ended)}"
        gs, ge = int(ended - 12), math.ceil(ended + 6)
        payload = self._payload(
            kind=LABEL_PICKUP, title=title, body=body, camera=camera, event_id=event_id,
            review_id=review_id, time_sensitive=not names,
            snapshot_url=f"{self.public}/api/{camera}/start/{gs}/end/{ge}/preview.gif" if self.public else "",
            thumbnail_url=(f"{self.public}/api/events/{event_id}/snapshot.jpg?bbox=1&crop=1&quality=70"
                           if self.public and event_id else ""),
            collapse=f"pickup-{event_id}"[:64], zones=self._zones_of(event_id))
        self.log(f"[smart] pickup on {camera}: {title!r}")
        self._post(payload)

    def _default_region_check(self, camera: str, ref_event: str, box: list) -> Optional[float]:
        if not ref_event or not box:
            return None
        ref = self._fb(f"/api/events/{ref_event}/snapshot.jpg?bbox=0&timestamp=0&crop=0&height=480")
        cur = self._fb(f"/api/{camera}/latest.jpg?height=480")
        a, b = gray_patch(ref, box), gray_patch(cur, box)
        if a is None or b is None:
            return None
        return ncc(a, b)

    # ---- vehicles -------------------------------------------------------------------------------

    def _home(self, zones) -> bool:
        return any(z in self.opts.vehicle_home_zones for z in zones or [])

    def _arrived_from_outside(self, o: Obj) -> bool:
        return bool(o.entered) and o.entered[0] not in self.opts.vehicle_home_zones

    def _identify(self, o: Obj) -> Optional[str]:
        if o.sub_label and o.sub_label in self.known:
            self.adopted[o.id] = o.sub_label
            # Anything the other driveway camera is tracking for the same moment is this vehicle too.
            for other in self.objs.values():
                if (other is not o and other.id not in self.adopted and other.camera != o.camera
                        and other.camera in self.opts.vehicle_cameras and other.label in VEHICLE_LABELS
                        and not (other.sub_label and other.sub_label in self.known)
                        and abs(other.start - o.start) <= _CROSS_CAM_S
                        and self._arrived_from_outside(other) == self._arrived_from_outside(o)):
                    self.adopted[other.id] = o.sub_label
            return o.sub_label
        if o.id in self.adopted:
            return self.adopted[o.id]
        for other in self.objs.values():
            name = other.sub_label if other.sub_label in self.known else None
            if (name and other.camera != o.camera and other.camera in self.opts.vehicle_cameras
                    and abs(other.start - o.start) <= _CROSS_CAM_S
                    and self._arrived_from_outside(other) == self._arrived_from_outside(o)):
                self.adopted[o.id] = name
                return name
        if o.box and o.entered and not self._arrived_from_outside(o):
            for name, v in self.vehicles.items():
                spot = (v.get("spots") or {}).get(o.camera)
                if v.get("status") == "home" and spot and iou(spot, o.box) >= _SPOT_IOU:
                    self.adopted[o.id] = name
                    return name
        return None

    def _vehicle_event(self, o: Obj, msg_type: str, now: float) -> None:
        if o.camera not in self.opts.vehicle_cameras:
            return
        in_home = self._home(o.current)
        if in_home:
            o.seen_home = True
            if o.stationary:
                o.stationary_home_at = now
                o.parked_changes = o.position_changes
        name = self._identify(o)
        if not name:
            return
        v = self.vehicles.setdefault(name, {"name": display_name(name), "status": "unknown",
                                            "since": 0, "changed": 0})
        silent = (o.first_seen - o.start) > _STALE_TRACK_S
        parked = in_home and (o.stationary or o.end is not None)
        if parked:
            if o.stationary and o.box:
                v.setdefault("spots", {})[o.camera] = [round(x, 4) for x in o.box]
            pend = self.depart_pending.get(name)
            if pend and o.stationary and now > pend["at"]:
                self.log(f"[smart] {name}: still parked on {o.camera}; departure cancelled")
                self.depart_pending.pop(name, None)
            if v.get("status") != "home":
                # The very track we announced leaving is parked in the driveway again: it never
                # left. Correct the state quietly rather than announce a second "arrived".
                arrived = self._arrived_from_outside(o) and o.id != v.get("depart_track")
                self._set_status(name, v, "home", o.start if silent else now, o,
                                 push=arrived and not silent and v.get("status") in ("away", "unknown")
                                 and not (v.get("status") == "unknown" and not v.get("changed")
                                          and o.first_seen - o.start > 120))
            elif (self._arrived_from_outside(o) and v.get("event_id") != o.id
                  and not silent and now - float(v.get("changed") or 0) > 600
                  and o.camera == v.get("camera")):
                # Home already, yet it just drove in: the departure was missed (a track lost on the
                # way out). Say so rather than stay quiet about a real arrival.
                self._set_status(name, v, "home", now, o, push=True)
            v["event_id"] = v.get("event_id") or o.id
            return
        # A parked car whose box merely jitters across a zone line (the LPR camera's zones have
        # inertia 1) stays `stationary` with the same position_changes in Frigate — measured on a
        # live 16-hour track. Leaving needs Frigate to have seen it actually move since it parked.
        moved = o.parked_changes < 0 or o.position_changes > o.parked_changes
        left = (o.seen_home and not in_home and not o.stationary and moved
                and (bool(o.current) or o.end is not None))
        if left and v.get("status") == "home" and name not in self.depart_pending:
            # Never silent, unlike an arrival: we only get here having WATCHED it go from inside the
            # home zones to outside them, so the exit is happening now even on a long-lived track.
            self.depart_pending[name] = {"track": o.id, "at": o.end or now}
            self.log(f"[smart] {name}: left the home zones on {o.camera}; confirming departure")

    def _set_status(self, name: str, v: dict, status: str, at: float, o: Obj, push: bool) -> None:
        v.update({"name": display_name(name), "status": status, "since": at, "changed": self.clock(),
                  "camera": o.camera, "event_id": o.id})
        if status == "away":
            # Spots are KEPT while away: a car pulling into that exact spot later is almost always
            # this vehicle with its plate unread (it was, on every unknown-car linger in a week).
            v["depart_track"] = o.id
        elif o.id != v.get("depart_track"):
            v.pop("depart_track", None)
        self._outbox.append(("vehicle_save", {}))
        if push:
            self._outbox.append(("vehicle", {"name": name, "status": status, "at": at,
                                             "camera": o.camera, "event_id": o.id}))
        self.log(f"[smart] {name} → {status}{'' if push else ' (silent)'}")

    def _vehicle_due(self, now: float) -> None:
        for name, p in list(self.depart_pending.items()):
            if now - p["at"] < self.opts.vehicle_depart_confirm_s:
                continue
            self.depart_pending.pop(name, None)
            v = self.vehicles.get(name) or {}
            # Still seen parked by any camera since the exit → it never left (turned around, or a
            # second car of the same identity).
            # (Including the exiting track itself: the LPR camera's box jitters across the zone line
            # while a car sits parked, and a real departure never parks back in the driveway.)
            still = [o for o in self.objs.values() if self.adopted.get(o.id) == name
                     and o.stationary_home_at > p["at"]]
            if still or v.get("status") != "home":
                continue
            track = self.objs.get(p["track"])
            if track is None:
                continue
            # A track we already announced leaving (then saw parked again) says it only once.
            self._set_status(name, v, "away", p["at"], track, push=p["track"] != v.get("depart_track"))

    def _send_vehicle(self, f: dict) -> None:
        name = f["name"]
        nice = display_name(name)
        verb = "arrived home" if f["status"] == "home" else "left"
        camera, eid = f["camera"], f["event_id"]
        review_id = self._review_for(eid)
        payload = self._payload(
            kind=LABEL_VEHICLE, title=f"{vehicle_emoji(name)} {nice} {verb}",
            body=f"{clock_text(f['at'])} · {titleize(camera)}", camera=camera, event_id=eid,
            review_id=review_id, time_sensitive=False,
            snapshot_url=(f"{self.public}/api/events/{eid}/snapshot.jpg?bbox=1&crop=1&quality=70"
                          if self.public else ""),
            thumbnail_url=f"{self.public}/api/events/{eid}/snapshot.jpg" if self.public else "",
            collapse=f"veh-{name}-{int(f['at'])}"[:64], zones=list(self.opts.vehicle_home_zones))
        payload["vehicle"] = name
        payload["vehicle_status"] = f["status"]
        self.log(f"[smart] vehicle push: {payload['title']!r}")
        self._post(payload)

    # ---- lingering ------------------------------------------------------------------------------

    def _linger_event(self, o: Obj, now: float) -> None:
        if o.label not in ({"person"} | VEHICLE_LABELS):
            return
        zones = linger_zones_for(self.opts.linger_zones, o.camera, o.label)
        if not zones:
            return
        in_zone = "*" in zones or any(z in zones for z in o.current)
        if in_zone:
            o.zone_out_since = None
            if o.zone_since is None:
                # A brand-new object already inside has been there since it started (Frigate assigns
                # zones on the first frame). One first seen MID-track (an add-on restart, or a long
                # silence) counts from now: its history is unknown, and backdating it would fire at
                # once for every car or person that was already there.
                fresh = o.first_seen == now and now - o.start < 30
                o.zone_since = o.start if fresh else now
        elif o.zone_since is not None:
            if o.zone_out_since is None:
                o.zone_out_since = now
            elif now - o.zone_out_since >= _LINGER_OUT_GRACE_S:
                o.zone_since = None
                o.zone_out_since = None

    def _linger_due(self, now: float) -> list:
        due = []
        taken = set()   # camera|class already due in this pass (the cooldown is stamped on fire)
        for o in self.objs.values():
            if o.end is not None or o.zone_since is None or o.zone_out_since is not None:
                continue
            if o.id in self.fired_linger:
                continue
            zones = linger_zones_for(self.opts.linger_zones, o.camera, o.label)
            is_person = o.label == "person"
            limit = self.opts.linger_person_s if is_person else self.opts.linger_car_s
            if now - o.zone_since < limit:
                continue
            if o.sub_label or o.label in CARRIERS or (o.attrs & CARRIERS):
                continue
            slot = f"{o.camera}|{is_person}"
            if slot in taken or now - self.linger_camera_at.get(slot, -1e18) < _LINGER_CAMERA_COOLDOWN_S:
                continue
            if is_person:
                if o.camera in self.opts.vehicle_cameras and any(
                        now - float(v.get("changed") or -1e18) < _HOUSEHOLD_ACTIVITY_S
                        and v.get("status") in ("home", "away") and float(v.get("since") or 0) > 0
                        and now - float(v.get("since") or 0) < _HOUSEHOLD_ACTIVITY_S
                        for v in self.vehicles.values()):
                    continue
                # A recognised household member standing with them makes this a visit, not a lurker.
                if any(p.camera == o.camera and p.label == "person" and p.sub_label and p.end is None
                       and now - p.last_seen < 30 for p in self.objs.values()):
                    continue
            else:
                if self.adopted.get(o.id) or (o.sub_label and o.sub_label in self.known):
                    continue
                # Only a car that drove in during this track: a parked household car's track restarts
                # every few hours, already inside the zone, with no plate read.
                if "*" in zones:
                    if o.position_changes < 1:
                        continue
                elif not o.entered or o.entered[0] in zones:
                    continue
                if o.household or (o.box and any(
                        same_spot(spot, o.box) for v in self.vehicles.values()
                        for cam, spot in (v.get("spots") or {}).items() if cam == o.camera)):
                    # Sticky: the same car pulling OUT of the spot hours later is still household.
                    o.household = True
                    continue
            due.append(o)
            taken.add(slot)
        return due

    def _confirm_linger(self, o: Obj, now: float) -> None:
        ev = self._fj(f"/api/events/{o.id}")
        if isinstance(ev, dict):
            if ev.get("end_time"):
                with self._lock:
                    o.end = float(ev["end_time"])
                return
            if _sub_label(ev.get("sub_label")):
                with self._lock:
                    o.sub_label = _sub_label(ev.get("sub_label"))
                return
        elif now - o.last_seen > _STALE_UPDATE_S:
            return   # can't confirm a track we haven't heard about in a while: never alert on a ghost
        with self._lock:
            if o.id in self.fired_linger:
                return
            self.fired_linger[o.id] = now
            self.linger_camera_at[f"{o.camera}|{o.label == 'person'}"] = now
            for k in [k for k, t in self.fired_linger.items() if now - t > _FIRED_TTL_S]:
                self.fired_linger.pop(k, None)
            fired = dict(self.fired_linger)
            since = o.zone_since or o.start
        self._set("smart:linger_fired", json.dumps(fired))
        place = PLACE_PHRASE.get(o.camera, f"at the {titleize(o.camera)}")
        who = "Someone has" if o.label == "person" else "An unknown car has"
        emoji = "\U0001f9cd" if o.label == "person" else "\U0001f697"
        title = f"{emoji} {who} been {place} for {minutes_text(now - since)}"
        body = f"{titleize(o.camera)} · since {clock_text(since)} · still there"
        review_id = self._review_for(o.id)
        payload = self._payload(
            kind=LABEL_LINGER, title=title, body=body, camera=o.camera, event_id=o.id,
            review_id=review_id, time_sensitive=True,
            snapshot_url=(f"{self.public}/api/events/{o.id}/snapshot.jpg?bbox=1&crop=1&quality=70"
                          if self.public else ""),
            thumbnail_url=f"{self.public}/api/events/{o.id}/snapshot.jpg" if self.public else "",
            collapse=f"linger-{o.id}"[:64], zones=list(o.current))
        payload["labels"].append(o.label)
        self.log(f"[smart] linger: {title!r}")
        self._post(payload)

    # ---- shared ---------------------------------------------------------------------------------

    def _review_for(self, event_id: str) -> str:
        if not event_id:
            return ""
        rv = self._fj(f"/api/review/event/{event_id}")
        return str(rv.get("id") or "") if isinstance(rv, dict) else ""

    def _zones_of(self, event_id: str) -> list:
        o = self.objs.get(event_id)
        return list(o.entered or o.current) if o else []

    def _payload(self, *, kind: str, title: str, body: str, camera: str, event_id: str, review_id: str,
                 time_sensitive: bool, snapshot_url: str, thumbnail_url: str, collapse: str,
                 zones: list) -> dict:
        return {
            "pairing_code": self.code,
            "title": title,
            "body": body,
            "camera": camera,
            "camera_name": titleize(camera),
            "review_id": review_id,
            "apex_url": (f"apex://review?id={review_id}" if review_id
                         else f"apex://event?id={event_id}" if event_id else ""),
            "collapse_id": collapse,
            "snapshot_url": snapshot_url,
            "thumbnail_url": thumbnail_url,
            "detection_id": event_id,
            "labels": [kind],
            "zones": zones,
            "kind": kind,
            "time_sensitive": time_sensitive,
            "frigate_base_url": self.public,
        }

    def tick(self) -> None:
        """Worker-thread heartbeat: housekeeping, then every decision that needs I/O."""
        self.refresh_config()
        now = self.clock()
        with self._lock:
            self._prune(now)
            pickups = self._package_due(now) if self.opts.package_alerts else []
            if self.opts.vehicle_alerts:
                self._vehicle_due(now)
            lingers = self._linger_due(now) if self.opts.linger_alerts else []
            outbox, self._outbox = self._outbox, []
        for s in pickups:
            try:
                self._confirm_pickup(s)
            except Exception as exc:  # noqa: BLE001 — one bad check must not stop the others
                self.log(f"[smart] pickup check failed: {exc!r}")
        saved = False
        for kind, facts in outbox:
            try:
                if kind == "vehicle_save":
                    if not saved:
                        self._save_vehicles()
                        saved = True
                elif kind == "vehicle":
                    self._send_vehicle(facts)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[smart] vehicle send failed: {exc!r}")
        for o in lingers:
            try:
                self._confirm_linger(o, now)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[smart] linger check failed: {exc!r}")

    def _prune(self, now: float) -> None:
        for oid in [k for k, o in self.objs.items()
                    if (o.end is not None and now - o.end > _KEEP_ENDED_S)
                    or (o.end is None and now - o.last_seen > 6 * 3600)]:
            self.objs.pop(oid, None)
            self.adopted.pop(oid, None)
        self.spots = [s for s in self.spots
                      if s.pending or s.tracks
                      or (s.armed and now - s.last_activity < 48 * 3600)
                      or (not s.armed and now - s.last_activity < 30 * 60)]

    def vehicles_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.vehicles))
