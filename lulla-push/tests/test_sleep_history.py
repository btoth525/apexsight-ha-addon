"""Segment + session assembly — the data behind the hypnogram."""
from app import sleep_history as sh
from app.sleep_history import AWAKE, DEEP, LIGHT, Segment


def seg(band, start, end):
    return Segment(band, float(start), float(end))


# ---- band_for ---------------------------------------------------------------------------

def test_band_follows_the_class_for_awake_not_the_stage():
    """The 5-minute class signal owns asleep-vs-awake so the chart can never contradict the
    sleep log or the notifications about a waking."""
    assert sh.band_for("awake", "light_sleep") == AWAKE
    assert sh.band_for("asleep", "deep_sleep") == DEEP


def test_a_stir_mid_nap_draws_as_light_not_awake():
    """The sock says 'awake' but the class hasn't confirmed it — she stirred. Drawing this as a
    waking is exactly the 70-a-night noise the debounce exists to remove."""
    assert sh.band_for("asleep", "awake") == LIGHT


def test_no_signal_is_a_gap_never_a_zero():
    assert sh.band_for("nosignal", "deep_sleep") is None
    assert sh.band_for("nosignal", None) is None


def test_asleep_without_a_stage_still_draws():
    assert sh.band_for("asleep", None) == LIGHT


# ---- segments_from_readings (the backfill path) ------------------------------------------

def test_backfill_matches_the_live_debounce_shape():
    """A 60s awake blip must NOT become a band — otherwise backfilled days would look different
    from days the poller wrote live, and the chart would change character 10 days back."""
    readings = [(t, "light_sleep") for t in range(0, 3600, 15)]
    readings += [(t, "awake") for t in range(3600, 3660, 15)]        # 60s blip
    readings += [(t, "light_sleep") for t in range(3660, 7200, 15)]
    bands = [s.band for s in sh.segments_from_readings(readings, already_gridded=True)]
    assert bands == [LIGHT]


def test_backfill_captures_a_real_wake():
    readings = [(t, "light_sleep") for t in range(0, 3600, 15)]
    readings += [(t, "awake") for t in range(3600, 7200, 15)]        # an hour awake
    segs = sh.segments_from_readings(readings, already_gridded=True)
    assert [s.band for s in segs] == [LIGHT, AWAKE]


def test_backfill_of_nothing_is_empty_not_an_error():
    assert sh.segments_from_readings([]) == []


# ---- sessions_from_segments -------------------------------------------------------------

def test_a_night_with_two_stirs_is_ONE_session_with_two_wakings():
    """The headline behaviour: a parent says 'she slept 8 to 6 and woke twice', not 'she had
    three sleeps'. The auto-log still records the stretches; this is the human view."""
    segs = [seg(LIGHT, 0, 3600), seg(DEEP, 3600, 5400), seg(AWAKE, 5400, 5700),
            seg(LIGHT, 5700, 9000), seg(AWAKE, 9000, 9300), seg(DEEP, 9300, 12600)]
    sessions = sh.sessions_from_segments(segs)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.wakings == 2
    assert s.asleep_seconds == 3600 + 1800 + 3300 + 3300
    assert s.deep_seconds == 1800 + 3300
    assert s.awake_seconds == 600


def test_a_long_awake_splits_the_night_into_two_sessions():
    segs = [seg(LIGHT, 0, 3600), seg(AWAKE, 3600, 3600 + sh.SESSION_GAP_SECONDS + 60),
            seg(LIGHT, 9000, 12600)]
    sessions = sh.sessions_from_segments(segs)
    assert len(sessions) == 2
    assert all(s.wakings == 0 for s in sessions)


def test_longest_stretch_is_the_unbroken_run_not_the_total():
    segs = [seg(LIGHT, 0, 1800), seg(AWAKE, 1800, 2100),
            seg(LIGHT, 2100, 5700), seg(DEEP, 5700, 7500)]
    s = sh.sessions_from_segments(segs)[0]
    assert s.longest_stretch == 3600 + 1800      # the second run, not 1800+3600+1800
    assert s.asleep_seconds == 1800 + 3600 + 1800


def test_a_session_never_starts_or_ends_on_awake():
    segs = [seg(AWAKE, 0, 600), seg(LIGHT, 600, 4200), seg(AWAKE, 4200, 4500)]
    s = sh.sessions_from_segments(segs)[0]
    assert s.start == 600 and s.end == 4200
    assert s.wakings == 0                         # the trailing awake is when she got up


def test_a_hole_in_the_data_ends_the_session():
    """Sock on the charger. We can't claim she slept through a stretch we weren't watching."""
    segs = [seg(LIGHT, 0, 3600), seg(LIGHT, 20000, 23600)]
    assert len(sh.sessions_from_segments(segs)) == 2


def test_sub_thirty_second_bands_are_dropped():
    segs = [seg(LIGHT, 0, 3600), seg(AWAKE, 3600, 3610), seg(LIGHT, 3610, 7200)]
    s = sh.sessions_from_segments(segs)[0]
    assert s.wakings == 0


def test_all_awake_produces_no_session():
    assert sh.sessions_from_segments([seg(AWAKE, 0, 3600)]) == []


def test_session_serializes_iso_dates_for_the_app():
    s = sh.sessions_from_segments([seg(LIGHT, 1_788_000_000, 1_788_003_600)])[0].as_dict()
    assert s["start"].endswith("Z") and s["segments"][0]["band"] == LIGHT
    assert s["asleep_seconds"] == 3600


def test_change_only_history_is_expanded_onto_the_poll_grid():
    """HA's recorder stores a row only when the state CHANGES. Without expanding that back into
    a regular sample stream, `debounce` never gets the second look it needs to confirm, and ten
    days collapse into one 22-hour "sleep" with zero wakings. That is exactly what happened."""
    change_only = [(0.0, "light_sleep"), (7200.0, "awake"),
                   (14400.0, "light_sleep"), (21600.0, "awake")]
    segs = sh.segments_from_readings(change_only)
    assert [s.band for s in segs] == [LIGHT, AWAKE, LIGHT]
    # Each band is confirmed one hold AFTER the raw change, which is the honest edge.
    assert segs[1].start == 7200.0 + sh.owlet_log.WAKE_HOLD_SECONDS
    # ...and the same input read literally (no expansion) confirms nothing at all: one row per
    # state gives `debounce` no second look, so the whole span collapses into a single band.
    assert len(sh.segments_from_readings(change_only, already_gridded=True)) <= 1
