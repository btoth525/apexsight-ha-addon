"""Pure classification tests for app/home.py — no network, no Supervisor needed."""
from app.home import classify


def s(entity_id, state, **attrs):
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


def test_no_matching_entities_returns_empty():
    out = classify([s("light.kitchen", "on"), s("sensor.outdoor_temp", "72")])
    assert out["vitals"] is None
    assert out["nursery"] == []


def test_owlet_vitals_assembled_from_separate_entities():
    states = [
        s("sensor.owlet_heart_rate", "128", friendly_name="Owlet Heart Rate"),
        s("sensor.owlet_oxygen_saturation", "99", friendly_name="Owlet SpO2"),
        s("sensor.owlet_skin_temperature", "98.1", friendly_name="Owlet Skin Temp"),
        s("sensor.owlet_battery_level", "82", friendly_name="Owlet Battery"),
        s("binary_sensor.owlet_charging", "off", friendly_name="Owlet Charging"),
        s("binary_sensor.owlet_sock_connection", "on", friendly_name="Owlet Sock Connected"),
    ]
    out = classify(states)
    v = out["vitals"]
    assert v == {"bpm": 128, "spo2": 99, "skin_temp_f": 98.1, "battery_pct": 82,
                 "sock_on": True, "charging": False}


def test_unavailable_owlet_entities_skipped_not_crashed():
    # All-unavailable means no Owlet data reachable at all — vitals stays None (not a
    # dict of nulls), so the app's card correctly hides rather than showing empty fields.
    states = [
        s("sensor.owlet_heart_rate", "unavailable"),
        s("sensor.owlet_oxygen_saturation", "unknown"),
    ]
    out = classify(states)
    assert out["vitals"] is None


def test_one_unavailable_owlet_field_among_others_is_left_unset():
    states = [
        s("sensor.owlet_heart_rate", "unavailable"),
        s("sensor.owlet_oxygen_saturation", "99"),
    ]
    out = classify(states)
    assert out["vitals"]["bpm"] is None
    assert out["vitals"]["spo2"] == 99


def test_no_owlet_entities_vitals_is_none():
    out = classify([s("sensor.living_room_temp", "70")])
    assert out["vitals"] is None


def test_nursery_entities_discovered_by_name():
    states = [
        s("sensor.nursery_temperature", "71", friendly_name="Nursery Temp", unit_of_measurement="°F"),
        s("sensor.nursery_humidity", "48", friendly_name="Nursery Humidity", unit_of_measurement="%"),
        s("switch.nursery_sound_machine", "on", friendly_name="Nursery Sound Machine"),
        s("light.nursery_lamp", "off", friendly_name="Nursery Lamp"),
        s("light.living_room", "on", friendly_name="Living Room"),   # NOT nursery — excluded
    ]
    out = classify(states)
    ids = {n["id"] for n in out["nursery"]}
    assert ids == {"sensor.nursery_temperature", "sensor.nursery_humidity",
                   "switch.nursery_sound_machine", "light.nursery_lamp"}
    sound = next(n for n in out["nursery"] if n["id"] == "switch.nursery_sound_machine")
    assert sound["is_toggle"] is True and sound["is_on"] is True and sound["value"] == "On"
    temp = next(n for n in out["nursery"] if n["id"] == "sensor.nursery_temperature")
    assert temp["is_toggle"] is False and temp["value"] == "71°F"


def test_nursery_matched_by_friendly_name_not_just_entity_id():
    # entity_id doesn't say "nursery" but the friendly name does — still discovered.
    out = classify([s("sensor.room_2_temp", "70", friendly_name="Nursery Temp")])
    assert len(out["nursery"]) == 1


def test_owlet_takes_priority_over_nursery_match():
    # An entity that matches BOTH heuristics is classified as Owlet, not double-counted.
    out = classify([s("sensor.nursery_owlet_heart_rate", "140")])
    assert out["vitals"]["bpm"] == 140
    assert out["nursery"] == []
