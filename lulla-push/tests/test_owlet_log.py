"""Pure sleep-state-machine tests (no clock/network/DB). Locks the auto-sleep-logging rules."""
from app import owlet_log as o


def test_sleep_class_mapping():
    assert o.sleep_class("awake") == "awake"
    assert o.sleep_class("Settling") == "awake"
    assert o.sleep_class("light") == "asleep"
    assert o.sleep_class("Deep") == "asleep"
    assert o.sleep_class("rem") == "asleep"
    assert o.sleep_class(None) == "nosignal"
    assert o.sleep_class("unavailable") == "nosignal"
    assert o.sleep_class("Not placed") == "nosignal"
    assert o.sleep_class("") == "nosignal"


def test_open_then_keep_then_close_writes_one_session():
    # asleep with nothing open -> start tracking, write nothing yet
    d = o.decide("asleep", None, "2026-07-27T01:00:00Z")
    assert d.new_open_start == "2026-07-27T01:00:00Z"
    assert d.write is None

    # still asleep 30 min later -> keep the same open start, still no write
    d = o.decide("asleep", "2026-07-27T01:00:00Z", "2026-07-27T01:30:00Z")
    assert d.new_open_start == "2026-07-27T01:00:00Z"
    assert d.write is None

    # wakes at 02:00 -> close + write the 1h session, clear the open cursor
    d = o.decide("awake", "2026-07-27T01:00:00Z", "2026-07-27T02:00:00Z")
    assert d.new_open_start is None
    assert d.write == {"start": "2026-07-27T01:00:00Z", "end": "2026-07-27T02:00:00Z"}


def test_short_blip_is_discarded_not_logged():
    # asleep for 2 minutes then awake -> below the 5-min floor, dropped (no nap logged)
    d = o.decide("awake", "2026-07-27T01:00:00Z", "2026-07-27T01:02:00Z")
    assert d.new_open_start is None
    assert d.write is None


def test_nosignal_closes_an_open_session():
    # sock comes off mid-sleep after 40 min -> still counts as a real sleep and is logged
    d = o.decide("nosignal", "2026-07-27T01:00:00Z", "2026-07-27T01:40:00Z")
    assert d.write == {"start": "2026-07-27T01:00:00Z", "end": "2026-07-27T01:40:00Z"}


def test_awake_with_nothing_open_does_nothing():
    d = o.decide("awake", None, "2026-07-27T12:00:00Z")
    assert d.new_open_start is None and d.write is None


def test_event_id_is_deterministic_per_session():
    a = o.session_event_id("2026-07-27T01:00:00Z")
    b = o.session_event_id("2026-07-27T01:00:00Z")
    c = o.session_event_id("2026-07-27T03:00:00Z")
    assert a == b and a != c


def test_payload_has_the_fields_the_app_decoder_requires():
    p = o.build_sleep_payload(start_iso="2026-07-27T01:00:00Z", end_iso="2026-07-27T02:00:00Z",
                              tz="America/Chicago", now_iso="2026-07-27T02:00:00Z")
    for key in ("id", "kindRaw", "startAt", "timezoneID", "sourceRaw",
                "diaperBlowout", "diaperRash", "createdBy", "createdAt", "updatedAt", "isTombstoned"):
        assert key in p, f"missing required field {key}"
    assert p["kindRaw"] == "sleep"
    assert p["sourceRaw"] == "owlet"
    assert p["childID"] is None
    assert p["isTombstoned"] is False
