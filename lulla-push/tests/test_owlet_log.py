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


def test_alert_transitions_are_edge_triggered():
    # nothing on -> low_o2 turns on: fires once
    assert o.alert_transitions({}, {"low_o2": True}) == ["low_o2"]
    # still on next poll: does NOT re-fire (edge-triggered, not level)
    assert o.alert_transitions({"low_o2": True}, {"low_o2": True}) == []
    # turning off doesn't fire
    assert o.alert_transitions({"low_o2": True}, {"low_o2": False}) == []
    # 'awake' is a sleep signal, never an alert
    assert o.alert_transitions({}, {"awake": True}) == []
    # an unknown key with no ALERT_META is ignored
    assert o.alert_transitions({}, {"mystery": True}) == []


def test_stage_change_detection():
    assert o.stage_changed("light", "deep") is True
    assert o.stage_changed("deep", "deep") is False, "same stage doesn't re-notify"
    assert o.stage_changed(None, "light") is False, "first reading seeds, no ping"
    assert o.stage_changed("light", "unavailable") is False, "going no-signal isn't a stage change"
    assert o.stage_changed("deep", "awake") is True, "waking is a stage change worth a ping"
    assert o.stage_label("deep_sleep") == "Deep Sleep"


def test_sleep_class_prefers_the_awake_flag():
    assert o.sleep_class_from_alerts({"awake": False}, None) == "asleep"
    assert o.sleep_class_from_alerts({"awake": True}, "deep") == "awake", "awake flag wins over text"
    assert o.sleep_class_from_alerts({"sock_off": True, "awake": False}, "deep") == "nosignal"
    # no awake flag -> fall back to the text state
    assert o.sleep_class_from_alerts({}, "light") == "asleep"
    assert o.sleep_class_from_alerts({}, None) == "nosignal"


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


# ---- debounce / hysteresis --------------------------------------------------------------
# Modelled on the real flap measured on Brandon's sock: light_sleep punctuated by ~60-120s
# "awake" blips, with genuine wakes running 8+ minutes.

from app.owlet_log import (Debounced, debounce, stage_alert_due, ALERTING_STAGES,
                           STAGE_ALERT_MIN_GAP, WAKE_HOLD_SECONDS)


def _run(readings, hold=WAKE_HOLD_SECONDS, start=None):
    """Feed (time, reading) pairs through the filter; return the list of confirmed edges."""
    state = Debounced() if start is None else start
    fired = []
    for now, reading in readings:
        state, confirmed = debounce(state, reading, now, hold)
        if confirmed:
            fired.append((now, confirmed))
    return state, fired


def test_first_reading_seeds_silently():
    """A cold start (or a fresh deploy) adopts the current state without announcing it."""
    state, fired = _run([(0, "asleep")])
    assert state.confirmed == "asleep"
    assert fired == []


def test_short_blip_is_swallowed():
    """The 60-second 'awake' twitch mid-nap — 43% of the sock's awake runs — never fires."""
    _, fired = _run([(0, "asleep"), (60, "awake"), (120, "asleep"), (180, "asleep")])
    # (60s is the single most common false-wake length in 14 days of real data.)
    assert fired == []


def test_two_minute_blip_is_also_swallowed():
    """43% of the sock's 'awake' runs are under 3 minutes; the hold must clear all of them."""
    _, fired = _run([(0, "asleep"), (10, "awake"), (100, "awake"), (134, "asleep")])
    assert fired == []


def test_sustained_change_confirms_once():
    """A real wake fires exactly one edge, `hold` seconds after it started."""
    start = 100
    ticks = [(0, "asleep")] + [(start + i * 15, "awake") for i in range(60)]
    state, fired = _run(ticks)
    assert [f[1] for f in fired] == ["awake"]
    assert fired[0][0] == start + WAKE_HOLD_SECONDS
    assert state.confirmed == "awake"


def test_flapping_restarts_the_clock():
    """Hysteresis needs the new state CONTINUOUSLY, so an interrupted run never confirms."""
    readings = []
    t = 0
    for _ in range(10):                  # 10 rounds of asleep/awake every 60s = 10 minutes
        readings += [(t, "asleep"), (t + 60, "awake")]
        t += 120
    _, fired = _run(readings)
    assert fired == []


def test_real_night_collapses_to_the_genuine_wakes():
    """The 04:01-04:34 window from HA's recorder: two false wakes (60s, 124s) then a real one."""
    readings = [(0, "asleep"),
                (700, "awake"), (760, "asleep"),            # 60s blip
                (1120, "awake"), (1244, "asleep"),          # 124s blip
                (2530, "awake")]
    readings += [(2530 + i * 15, "awake") for i in range(1, 140)]   # the real 34-minute wake
    _, fired = _run(readings)
    assert [f[1] for f in fired] == ["awake"]
    assert fired[0][0] == 2530 + WAKE_HOLD_SECONDS


def test_none_reading_holds_the_timer():
    """A dropped poll (sock unavailable) must not reset a candidate that's mid-count."""
    state = Debounced(confirmed="asleep", candidate="awake", since=0.0)
    state, fired = debounce(state, None, 100, WAKE_HOLD_SECONDS)
    assert fired is None
    assert state.candidate == "awake" and state.since == 0.0


def test_state_survives_a_restart():
    state = Debounced(confirmed="deep_sleep", candidate="light_sleep", since=42.0)
    assert Debounced.from_json(state.to_json()) == state


def test_legacy_bare_string_upgrades_without_reannouncing():
    """1.3.0 stored `owlet_stage` as a bare value; reading it as `confirmed` keeps deploy quiet."""
    assert Debounced.from_json("light_sleep").confirmed == "light_sleep"
    assert Debounced.from_json(None) == Debounced()
    assert Debounced.from_json("{not json").confirmed == "{not json"


def test_stage_alerts_are_rate_limited_per_stage():
    """1.3.0's single shared 10-minute budget let a light-sleep ping DROP the deep-sleep one."""
    last = {"light_sleep": 1000.0}
    assert stage_alert_due(last, "deep_sleep", 1001.0) is True     # different stage, unaffected
    assert stage_alert_due(last, "light_sleep", 1001.0) is False   # same stage, too soon
    assert stage_alert_due(last, "light_sleep", 1000.0 + STAGE_ALERT_MIN_GAP) is True
    assert stage_alert_due({}, "deep_sleep", 0.0) is True          # never fired before


def test_only_deep_sleep_is_worth_announcing():
    """Light sleep is where a newborn spends most of the night — announcing it added ~16
    pushes a day in the replay and told Taylor nothing she couldn't see on the widget."""
    assert "deep_sleep" in ALERTING_STAGES
    assert "light_sleep" not in ALERTING_STAGES


# ---- deep-sleep arm helper (transfer window) --------------------------------------------
# The arm/fire/expire decision is inlined in the poller, but the raw-stage EDGE detection it
# depends on is `stage_changed`-like: fire only on a FRESH entry into deep sleep, never while
# already deep. These pin that contract so a refactor of the poller can't silently break it.

from app.owlet_log import deep_arm_decision


def test_arm_fires_on_a_fresh_deep_entry():
    assert deep_arm_decision(armed_until=1000.0, now=500.0, prev_stage="light_sleep",
                             cur_stage="deep_sleep", sleep_class_confirmed="asleep") == "fire"


def test_arm_does_not_refire_while_already_deep():
    """One shot: being deep for a while must not keep alerting."""
    assert deep_arm_decision(armed_until=1000.0, now=500.0, prev_stage="deep_sleep",
                             cur_stage="deep_sleep", sleep_class_confirmed="asleep") == "hold"


def test_arm_ignores_a_raw_deep_flicker_around_a_wake():
    """The raw stage can flick to deep for one poll while she's actually waking; the confirmed
    class gates it out so a flicker never fires the 'safe to put down' alert."""
    assert deep_arm_decision(armed_until=1000.0, now=500.0, prev_stage="light_sleep",
                             cur_stage="deep_sleep", sleep_class_confirmed="awake") == "hold"


def test_arm_expires_on_its_own():
    """A forgotten arm must never ping hours later."""
    assert deep_arm_decision(armed_until=1000.0, now=1001.0, prev_stage="light_sleep",
                             cur_stage="deep_sleep", sleep_class_confirmed="asleep") == "expire"


def test_unarmed_is_always_a_hold():
    assert deep_arm_decision(armed_until=0.0, now=500.0, prev_stage="light_sleep",
                             cur_stage="deep_sleep", sleep_class_confirmed="asleep") == "hold"
