"""Daily summaries: the evening "Your Day" and the morning "Overnight" brief.

The bridge accumulates each Frigate event into the shared DB straight from the
MQTT stream, so both are built locally with no HTTP call to Frigate (and no
auth). Written as plain sentences, the way you'd tell someone what happened.
"""
import datetime
from typing import Optional

CARRIERS = {
    "amazon", "ups", "usps", "fedex", "dhl", "an_post", "purolator",
    "dpd", "gls", "postnl", "postnord", "canada_post", "royal_mail",
}
CARRIER_NAMES = {"ups": "UPS", "usps": "USPS", "fedex": "FedEx", "dhl": "DHL", "dpd": "DPD", "gls": "GLS"}
ANIMALS = {"dog", "cat", "bird", "horse", "deer", "raccoon", "fox", "squirrel", "bear"}
VEHICLES = {"car", "truck", "motorcycle", "bus"}


def _titleize(s: str) -> str:
    return s.replace("_", " ").title() if s else s


def _carrier_name(s: str) -> str:
    return CARRIER_NAMES.get(s, _titleize(s))


def _join(items: list) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _clock(ts: float, tz: Optional[datetime.tzinfo]) -> str:
    t = datetime.datetime.fromtimestamp(ts, tz)
    return t.strftime("%I:%M %p").lstrip("0")


def _row(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _tally(rows: list) -> dict:
    names: dict[str, int] = {}
    pets: dict[str, int] = {}
    carriers: dict[str, int] = {}
    unknown_people: list = []
    animals = 0
    vehicles = 0
    packages = 0
    for row in rows:
        label = (_row(row, "label") or "").lower()
        sub = (_row(row, "sub_label") or "").strip()
        low = sub.lower()
        if low in CARRIERS or label in CARRIERS:
            key = low if low in CARRIERS else label
            carriers[key] = carriers.get(key, 0) + 1
        elif label == "person":
            if sub:
                names[sub] = names.get(sub, 0) + 1
            else:
                unknown_people.append(row)
        elif label in ANIMALS:
            animals += 1
            if sub:
                pets[sub] = pets.get(sub, 0) + 1
        elif label in VEHICLES:
            vehicles += 1
        elif label == "package":
            packages += 1
    return {"names": names, "pets": pets, "carriers": carriers, "unknown": unknown_people,
            "animals": animals, "vehicles": vehicles, "packages": packages}


def format_evening(rows: list) -> tuple[str, str]:
    """(title, body) for the day: who came by, what was delivered, the pets, then a nudge to the film."""
    total = len(rows)
    if total == 0:
        return ("Your Day", "A quiet day. Nothing on the cameras today.")
    t = _tally(rows)
    sentences: list[str] = []
    if t["names"]:
        people = sorted(t["names"], key=lambda n: -t["names"][n])[:3]
        sentences.append(f"{_join([_titleize(n) for n in people])} came by.")
    visitors = len(t["unknown"])
    if visitors:
        sentences.append("Someone you don't know showed up once." if visitors == 1
                         else f"{visitors} visits from people you don't know.")
    if t["carriers"]:
        names = [_carrier_name(c) for c in sorted(t["carriers"], key=lambda c: -t["carriers"][c])[:3]]
        sentences.append(f"{_join(names)} came by with deliveries.")
    elif t["packages"]:
        sentences.append("A package showed up." if t["packages"] == 1 else f"{t['packages']} packages showed up.")
    if t["pets"]:
        pet, count = max(t["pets"].items(), key=lambda kv: kv[1])
        sentences.append(f"{_titleize(pet)} was out {count} time{'' if count == 1 else 's'}.")
    sentences.append(f"{total} moment{'' if total == 1 else 's'} in all. Tap to watch your day.")
    return ("Your Day", " ".join(sentences))


# Kept for the catch-up path and older callers.
def format_recap(rows: list) -> Optional[tuple[str, str]]:
    return format_evening(rows)


def format_morning(rows: list, tz: Optional[datetime.tzinfo] = None) -> tuple[str, str]:
    """(title, body) for the night: a quiet night says so; otherwise the people first (with times),
    then cars and animals. Deliveries don't happen overnight, so they're folded into people."""
    t = _tally(rows)
    people = sorted([r for r in rows if (_row(r, "label") or "").lower() == "person"],
                    key=lambda r: _row(r, "ts") or 0)
    if not people and not t["vehicles"] and not t["animals"]:
        return ("Good Morning", "A quiet night. Nothing came by.")
    sentences: list[str] = []
    for row in people[:3]:
        sub = (_row(row, "sub_label") or "").strip()
        who = _titleize(sub) if sub and sub.lower() not in CARRIERS else "Someone"
        where = _titleize(_row(row, "camera") or "")
        ts = _row(row, "ts")
        when = f" at {_clock(ts, tz)}" if ts else ""
        sentences.append(f"{who} at the {where}{when}." if where else f"{who}{when}.")
    if len(people) > 3:
        sentences.append(f"{len(people) - 3} more sighting{'' if len(people) == 4 else 's'} of people.")
    extras = []
    if t["vehicles"]:
        extras.append("a car" if t["vehicles"] == 1 else f"{t['vehicles']} cars")
    if t["animals"]:
        extras.append("an animal" if t["animals"] == 1 else f"{t['animals']} animals")
    if extras:
        sentences.append(f"Also {_join(extras)}." if people else f"Nobody came by, just {_join(extras)}.")
    return ("Overnight" if people else "Good Morning", " ".join(sentences))
