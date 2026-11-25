"""Shared fakes for the smart-alert suites (1.31.0): a clock, a Frigate, a store, and MQTT-shaped
event builders. Synthetic data only — shaped like real `frigate/events` payloads (pixel boxes,
`[name, score]` sub_labels, ordered `entered_zones`), with no real names or plates."""
import json

import smart_alerts

DIMS = {"doorbell": (768, 1024), "Front_Driveway": (2560, 720), "Front_Driveway_LPR": (2048, 576),
        "Side_Gate": (1920, 1080)}

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


def finish():
    print(f"\n{sum(ok)}/{len(ok)} passed")
    raise SystemExit(0 if all(ok) else 1)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class World:
    """One SmartAlerts with fake I/O. `events` = what GET /api/events/<id> returns."""

    def __init__(self, opts=None, known=("Family_Truck", "Family_Jeep"), ncc=None):
        self.clock = Clock()
        self.posts = []
        self.store = {}
        self.events = {}
        self.reviews = {}
        self.ncc = ncc            # value the image check returns (None = unavailable)
        self.ncc_calls = 0
        self.config = {"cameras": {c: {"detect": {"width": w, "height": h}} for c, (w, h) in DIMS.items()},
                       "lpr": {"known_plates": {k: ["XXX-0000"] for k in known} | {"ups": []}}}
        self.sa = smart_alerts.SmartAlerts(
            opts or smart_alerts.Options(), post=self.posts.append, frigate_json=self._json,
            frigate_bytes=lambda path: None, store_get=self.store.get,
            store_set=self.store.__setitem__, public_base="https://nvr.example", pairing_code="APEX-TEST-0000",
            clock=self.clock, log=lambda *a: None, region_check=self._region)
        self.sa.refresh_config(force=True)

    def _json(self, path):
        if path == "/api/config":
            return self.config
        if path.startswith("/api/review/event/"):
            eid = path.rsplit("/", 1)[1]
            return {"id": self.reviews[eid]} if eid in self.reviews else None
        if path.startswith("/api/events/"):
            return self.events.get(path.rsplit("/", 1)[1])
        return None

    def _region(self, camera, ref, box):
        self.ncc_calls += 1
        return self.ncc

    def send(self, msg_type, **after):
        self.sa.on_event(msg_type, after)

    def tick(self, seconds=0):
        if seconds:
            self.clock.advance(seconds)
        self.sa.tick()

    def run(self, seconds, step=5):
        """Advance in steps, ticking each time (like the bridge's 5 s heartbeat)."""
        for _ in range(int(seconds / step)):
            self.tick(step)


def obj(oid, camera, label, start, box_px, *, sub=None, entered=None, current=None, stationary=False,
        moves=0, end=None, attrs=None):
    a = {"id": oid, "camera": camera, "label": label, "start_time": start, "end_time": end,
         "box": list(box_px), "sub_label": [sub, 0.95] if sub else None,
         "entered_zones": list(entered or []), "current_zones": list(current or []),
         "stationary": stationary, "active": not stationary, "position_changes": moves,
         "current_attributes": [{"label": x, "score": 0.9, "box": [0, 0, 1, 1]} for x in (attrs or [])]}
    return a


def vehicles_state(world):
    raw = world.store.get("vehicles:APEX-TEST-0000")
    return json.loads(raw) if raw else {}
