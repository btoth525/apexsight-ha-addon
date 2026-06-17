"""Daily recap formatter.

The bridge accumulates each Frigate event into the shared DB straight from the
MQTT stream, so the recap is built locally with no HTTP call to Frigate (and no
auth). Mirrors the in-app DailyRecap wording.
"""
from typing import Optional

CARRIERS = {
    "amazon", "ups", "usps", "fedex", "dhl", "an_post", "purolator",
    "dpd", "gls", "postnl", "postnord", "canada_post", "royal_mail",
}


def _titleize(s: str) -> str:
    return s.replace("_", " ").title() if s else s


def format_recap(rows: list) -> Optional[tuple[str, str]]:
    """(title, body) for the day's accumulated events. `rows` have camera/label/sub_label."""
    total = len(rows)
    if total == 0:
        return ("📊 Daily Recap", "All quiet today — no camera activity.")

    cameras: dict[str, int] = {}
    people: set[str] = set()
    carriers: dict[str, int] = {}
    packages = 0
    for row in rows:
        cam = row["camera"] or ""
        if cam:
            cameras[cam] = cameras.get(cam, 0) + 1
        label = (row["label"] or "").lower()
        sub = row["sub_label"]
        if label == "person" and sub:
            people.add(sub)
        if label == "package":
            packages += 1
        if sub and sub.lower() in CARRIERS:
            carriers[sub.lower()] = carriers.get(sub.lower(), 0) + 1

    title = f"📊 Daily Recap — {total} event{'' if total == 1 else 's'}"

    bits: list[str] = []
    if people:
        bits.append("Seen: " + ", ".join(_titleize(p) for p in sorted(people)[:3]))
    if cameras:
        top = max(cameras.items(), key=lambda kv: kv[1])
        bits.append(f"Busiest: {_titleize(top[0])} ({top[1]})")
    if carriers:
        bits.append(", ".join(f"{_titleize(name)} 📦"
                              for name, _ in sorted(carriers.items(), key=lambda kv: -kv[1])[:3]))
    elif packages:
        bits.append(f"{packages} 📦")
    body = " · ".join(bits) if bits else f"{total} events across {len(cameras)} cameras."
    return (title, body)
