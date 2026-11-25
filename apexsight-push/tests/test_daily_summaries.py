"""Evening "Your Day" and morning "Overnight" wording, from the rows the bridge accumulates."""
import datetime

from app import recap

TZ = datetime.timezone(datetime.timedelta(hours=-6))


def row(camera, label, sub="", ts=0.0):
    return {"camera": camera, "label": label, "sub_label": sub, "ts": ts}


def at(hour, minute=0):
    return datetime.datetime(2026, 11, 25, hour, minute, tzinfo=TZ).timestamp()


def test_evening_tells_the_day():
    rows = [row("doorbell", "person", "Brandon"), row("doorbell", "person", "Brandon"),
            row("Front_Driveway", "person", "Taylor"), row("doorbell", "person"),
            row("Front_Driveway", "car", "ups"), row("Front_Driveway", "car", "amazon"),
            row("Backyard_Wide", "dog", "Chico"), row("Backyard_Wide", "dog", "Chico")]
    title, body = recap.format_evening(rows)
    assert title == "Your Day"
    assert "Brandon and Taylor came by." in body
    assert "Someone you don't know showed up once." in body
    assert "came by with deliveries." in body and "UPS" in body and "Amazon" in body
    assert "Chico was out 2 times." in body
    assert body.endswith("8 moments in all. Tap to watch your day.")
    assert "📊" not in title


def test_evening_quiet_day():
    assert recap.format_evening([]) == ("Your Day", "A quiet day. Nothing on the cameras today.")


def test_morning_quiet_night():
    title, body = recap.format_morning([row("Garage", "car", ts=at(2))][:0], TZ)
    assert (title, body) == ("Good Morning", "A quiet night. Nothing came by.")


def test_morning_lists_people_with_times():
    rows = [row("Side_Gate", "person", ts=at(1, 12)), row("doorbell", "person", "Taylor", ts=at(0, 5)),
            row("Front_Driveway", "car", ts=at(3)), row("Front_Driveway", "car", ts=at(4))]
    title, body = recap.format_morning(rows, TZ)
    assert title == "Overnight"
    assert body.startswith("Taylor at the Doorbell at 12:05 AM. Someone at the Side Gate at 1:12 AM.")
    assert "Also 2 cars." in body


def test_morning_cars_only_is_still_a_good_morning():
    title, body = recap.format_morning([row("Front_Driveway", "car", ts=at(2))], TZ)
    assert title == "Good Morning"
    assert body == "Nobody came by, just a car."


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("daily summaries: ok")
