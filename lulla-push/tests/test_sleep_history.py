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


def test_backfill_extends_the_final_state_to_now():
    """HA's change-only history's last row is the CURRENT state, held until now. Without
    extending it, the backfill leaves a gap that could split the in-progress session from its
    own history."""
    # A single light_sleep change 2h ago; `until` = now.
    now = 10_000.0
    readings = [(now - 7200, "light_sleep")]
    segs = sh.segments_from_readings(readings, until=now)
    assert segs, "the final held state must produce a band"
    assert segs[-1].end >= now - 60          # reaches (about) now, not just +15s after the change


# ---- Owlet-matched per-minute sessions --------------------------------------------------
from app.sleep_history import (owlet_sessions, _count_wakings, _minute_state,
                               WAKE_REGISTER_MINUTES, WAKE_REARM_MINUTES)


def _minutes(spec, start=1_788_000_000):
    """spec: list of (state, count_minutes) -> a per-minute (ts, state) list."""
    out = []
    t = start
    for state, n in spec:
        for _ in range(n):
            out.append((t, state)); t += 60
    return out


def test_minute_state_normalizes_vocabulary():
    assert _minute_state("deep_sleep") == "deep_sleep"
    assert _minute_state("awake") == "awake"
    assert _minute_state("unavailable") == "nosignal"
    assert _minute_state(None) == "nosignal"


def test_waking_needs_sustained_awake_not_a_stir():
    # 30 asleep, 1 awake (a stir), 30 asleep -> NOT a waking (< 5 min awake)
    states = ["light_sleep"] * 30 + ["awake"] + ["light_sleep"] * 30
    assert _count_wakings(states) == 0
    # 30 asleep, 6 awake, 30 asleep -> ONE waking
    states = ["light_sleep"] * 30 + ["awake"] * 6 + ["light_sleep"] * 30
    assert _count_wakings(states) == 1


def test_waking_does_not_recount_until_rearmed():
    # sustained wake, brief sleep (< re-arm), sustained wake -> still ONE waking
    states = (["light_sleep"] * 20 + ["awake"] * 6 + ["light_sleep"] * 3
              + ["awake"] * 6 + ["light_sleep"] * 20)
    assert _count_wakings(states) == 1
    # ...but a full re-arm of sleep between them -> TWO
    states = (["light_sleep"] * 20 + ["awake"] * 6 + ["light_sleep"] * (WAKE_REARM_MINUTES + 1)
              + ["awake"] * 6 + ["light_sleep"] * 20)
    assert _count_wakings(states) == 2


def test_leading_and_trailing_awake_are_not_wakings():
    # settling before sleep + final wake after -> zero wakings
    states = ["awake"] * 40 + ["light_sleep"] * 60 + ["awake"] * 40
    assert _count_wakings(states) == 0


def test_one_sock_session_spans_brief_sock_off():
    # asleep, 5-min sock-off (bridged), asleep -> ONE session, not two
    m = _minutes([("light_sleep", 60), ("nosignal", 5), ("light_sleep", 60)])
    sess = owlet_sessions(m)
    assert len(sess) == 1
    assert sess[0].asleep_minutes == 120


def test_long_sock_off_splits_sessions():
    # Over NIGHT_MERGE_GAP_MINUTES of sock-off really is two sleeps. (Until 1.11.0 this test used
    # a 30-minute gap; that now merges into one night — see the tests below.)
    m = _minutes([("light_sleep", 60), ("nosignal", 60), ("light_sleep", 60)])
    sess = owlet_sessions(m)
    assert len(sess) == 2


def test_session_stats_are_owlet_shaped():
    # 20 deep + 60 light + 6 awake(one waking) + 40 light
    m = _minutes([("deep_sleep", 20), ("light_sleep", 60), ("awake", 6), ("light_sleep", 40)])
    s = owlet_sessions(m)[0]
    assert s.deep_minutes == 20
    assert s.light_minutes == 100
    assert s.asleep_minutes == 120           # light + deep, excludes awake
    assert s.awake_minutes == 6
    assert s.wakings == 1
    assert s.longest_stretch_minutes == 80   # the 20 deep + 60 light run, before the wake
    d = s.as_dict()
    assert d["asleep_seconds"] == 120 * 60 and d["deep_seconds"] == 20 * 60


# ---- one night across a sock-off gap (1.11.0) ---------------------------------------------
from app.sleep_history import NIGHT_MERGE_GAP_MINUTES, SESSION_BRIDGE_MINUTES, merge_bands

T0 = 1_788_000_000


def test_a_30_minute_sock_off_is_one_night_with_the_gap_uncovered():
    m = _minutes([("light_sleep", 60), ("nosignal", 30), ("deep_sleep", 60)], start=T0)
    sess = owlet_sessions(m)
    assert len(sess) == 1
    s = sess[0]
    assert s.start == T0 and s.end == T0 + 150 * 60          # spans both halves AND the gap
    # Totals are REAL minutes only — the 30 sock-off minutes are in none of them.
    assert (s.asleep_minutes, s.light_minutes, s.deep_minutes, s.awake_minutes) == (120, 60, 60, 0)
    assert s.longest_stretch_minutes == 60                    # the gap breaks the stretch
    assert s.wakings == 0                                      # a gap is never a waking
    # No band covers the gap: it's a hole between bands, at the right offset.
    gap = (T0 + 60 * 60, T0 + 90 * 60)
    assert all(b.end <= gap[0] or b.start >= gap[1] for b in s.segments)
    assert [(b.band, b.start, b.end) for b in s.segments] == [
        ("light_sleep", T0, T0 + 3600), ("deep_sleep", T0 + 5400, T0 + 9000)]
    # ...and nothing on the wire invents a kind the app can't decode.
    assert {seg["band"] for seg in s.as_dict()["segments"]} <= {"light_sleep", "deep_sleep", "awake"}


def test_merge_gap_boundary_45_merges_46_splits():
    assert NIGHT_MERGE_GAP_MINUTES == 45
    at = _minutes([("light_sleep", 60), ("nosignal", 45), ("light_sleep", 60)])
    over = _minutes([("light_sleep", 60), ("nosignal", 46), ("light_sleep", 60)])
    assert len(owlet_sessions(at)) == 1
    assert len(owlet_sessions(over)) == 2


def test_a_gap_with_no_rows_at_all_merges_too():
    """Relay down / no rows written: past the 15-minute carry it's no-data, not sleep."""
    first = _minutes([("light_sleep", 60)], start=T0)
    second = _minutes([("light_sleep", 60)], start=T0 + 90 * 60)     # 30 minutes with no rows
    s = owlet_sessions(first + second)
    assert len(s) == 1 and s[0].asleep_minutes == 120
    assert s[0].longest_stretch_minutes == 60


def test_sunday_9_27_is_one_night_not_two():
    """The live regression: 9:10 PM–12:11 AM, sock on the charger 19 minutes for a feed,
    12:30–9:16 AM. Was two sessions; is one night from 9:10 PM to 9:16 AM."""
    first = [("light_sleep", 40), ("deep_sleep", 60), ("awake", 6), ("light_sleep", 70),
             ("awake", 5)]                                        # 181 min: 9:10 PM -> 12:11 AM
    second = [("awake", 4), ("light_sleep", 200), ("awake", 8), ("deep_sleep", 100),
              ("light_sleep", 200), ("awake", 14)]                # 526 min: 12:30 AM -> 9:16 AM
    m = _minutes(first + [("nosignal", 19)] + second, start=T0)
    sess = owlet_sessions(m)
    assert len(sess) == 1
    s = sess[0]
    assert s.start == T0 and s.end == T0 + (181 + 19 + 526) * 60
    split = owlet_sessions(m, merge_gap_minutes=SESSION_BRIDGE_MINUTES)   # the 1.10.1 behaviour
    assert len(split) == 2
    # Totals of the merged night are exactly the two halves' real minutes added up.
    for f in ("asleep_minutes", "light_minutes", "deep_minutes", "awake_minutes"):
        assert getattr(s, f) == sum(getattr(x, f) for x in split)


def test_merged_night_counts_the_feed_between_the_halves_as_a_waking():
    """Split, the first half's trailing awake and the second's leading awake were each the edge
    of a session and never counted. As one night she woke up in the middle of it — Owlet's
    rule counts a sustained wake between sleep bouts. (A count change that comes from the merge,
    not from the waking rule itself.)"""
    m = _minutes([("light_sleep", 60), ("awake", 6), ("nosignal", 20), ("awake", 2),
                  ("light_sleep", 60)])
    split = owlet_sessions(m, merge_gap_minutes=SESSION_BRIDGE_MINUTES)
    assert [x.wakings for x in split] == [0, 0]
    assert owlet_sessions(m)[0].wakings == 1


def test_a_fragment_between_two_halves_is_absorbed_with_its_real_minutes():
    m = _minutes([("light_sleep", 60), ("nosignal", 17), ("light_sleep", 5), ("nosignal", 17),
                  ("light_sleep", 60)])
    s = owlet_sessions(m)
    assert len(s) == 1 and s[0].asleep_minutes == 125


def test_a_fragment_alone_after_a_night_does_not_stretch_the_night():
    """A 3-minute 'sleep' misread during a morning feed isn't a session, so it can't extend
    the night it follows."""
    m = _minutes([("light_sleep", 120), ("nosignal", 20), ("light_sleep", 3)], start=T0)
    s = owlet_sessions(m)
    assert len(s) == 1 and s[0].end == T0 + 120 * 60


def test_awake_only_sock_time_before_bed_is_not_pulled_into_the_night():
    m = _minutes([("awake", 15), ("nosignal", 20), ("light_sleep", 120)], start=T0)
    s = owlet_sessions(m)
    assert len(s) == 1 and s[0].start == T0 + 35 * 60 and s[0].awake_minutes == 0


# ---- _count_wakings: the comment was fixed, the counts must not move ------------------------

def _wakings_fixture(seed, n=720):
    import random
    rng = random.Random(seed)
    states = []
    kinds = ["light_sleep", "deep_sleep", "awake", "nosignal"]
    while len(states) < n:
        states += [rng.choices(kinds, [0.45, 0.3, 0.2, 0.05])[0]] * rng.randint(1, 12)
    return states[:n]


def test_waking_counts_on_fixtures_are_unchanged_from_1_10_1():
    # Captured from 1.10.1's `_count_wakings` before the comment fix.
    golden = [10, 14, 12, 11, 10, 14, 11, 9, 10, 13, 9, 11]
    assert [_count_wakings(_wakings_fixture(s)) for s in range(12)] == golden


def test_a_sock_off_minute_breaks_an_awake_run():
    """3 + 3 observed awake minutes either side of a sock-off are not 5 continuous ones."""
    states = ["light_sleep"] * 20 + ["awake"] * 3 + ["nosignal"] + ["awake"] * 3 + ["light_sleep"] * 20
    assert _count_wakings(states) == 0


def test_a_sock_off_minute_pauses_but_does_not_reset_re_arming():
    """5 + 5 asleep minutes either side of a sock-off re-arm (10), so the second wake counts."""
    states = (["light_sleep"] * 20 + ["awake"] * 6 + ["light_sleep"] * 5 + ["nosignal"] * 3
              + ["light_sleep"] * 5 + ["awake"] * 6 + ["light_sleep"] * 20)
    assert _count_wakings(states) == 2


# ---- merge_bands: fewer bands, same coverage ------------------------------------------------

def _night(n_bands, start=T0, seed=7):
    """A synthetic busy night of `n_bands` contiguous 1–4 minute runs, alternating kinds, with
    a few real (>=5 min) wakings in it."""
    import random
    rng = random.Random(seed)
    out, t, prev = [], float(start), None
    for i in range(n_bands):
        if i % 37 == 18:
            kind, minutes = AWAKE, rng.randint(5, 9)           # a real waking
        else:
            kind = rng.choice([k for k in (LIGHT, DEEP, AWAKE) if k != prev])
            minutes = rng.randint(1, 4) if kind != AWAKE else rng.randint(1, 3)
        if kind == prev:
            kind = LIGHT if prev != LIGHT else DEEP
        out.append(seg(kind, t, t + minutes * 60))
        t += minutes * 60
        prev = kind
    return out


def _covered(bands):
    return sum(b.end - b.start for b in bands)


def test_merge_is_a_no_op_when_it_already_fits():
    night = _night(40)
    assert merge_bands(night, 160) == night


def test_merge_covers_the_whole_span_with_no_holes():
    night = _night(300)
    for cap in (160, 90, 40, 12):
        out = merge_bands(night, cap)
        assert len(out) <= cap
        assert out[0].start == night[0].start and out[-1].end == night[-1].end
        assert all(a.end == b.start for a, b in zip(out, out[1:]))      # contiguous: no skips
        assert _covered(out) == _covered(night)


def test_merge_never_paints_a_real_waking_as_sleep():
    night = _night(300)
    wakes = [b for b in night if b.band == AWAKE and b.seconds >= 300]
    assert wakes
    out = merge_bands(night, 40)
    for w in wakes:
        holder = [b for b in out if b.start <= w.start and w.end <= b.end]
        assert len(holder) == 1 and holder[0].band == AWAKE


def test_merged_kind_is_the_dominant_one_and_ties_go_to_awake():
    assert merge_bands([seg(LIGHT, 0, 600), seg(DEEP, 600, 660), seg(LIGHT, 660, 1260)], 1) == \
        [seg(LIGHT, 0, 1260)]
    assert merge_bands([seg(LIGHT, 0, 120), seg(AWAKE, 120, 240)], 1) == [seg(AWAKE, 0, 240)]


def test_merge_across_a_gap_keeps_start_and_end_and_counts_only_real_seconds():
    # 2 min light, 30 min hole, 1 min awake: light wins on REAL seconds, not on span.
    out = merge_bands([seg(LIGHT, 0, 120), seg(AWAKE, 1920, 1980)], 1)
    assert out == [seg(LIGHT, 0, 1980)]


def test_merge_stops_rather_than_hide_wakings():
    """Every other band a 5-minute waking between 10-minute sleeps: there is no legal merge,
    so the list comes back as-is and the caller (the push builder) decides."""
    night, t = [], 0.0
    for i in range(20):
        kind, minutes = (AWAKE, 5) if i % 2 else (LIGHT, 10)
        night.append(seg(kind, t, t + minutes * 60)); t += minutes * 60
    assert merge_bands(night, 5) == night


def test_merge_is_fast_enough_for_the_poll_loop():
    import time as _t
    night = _night(900)
    t0 = _t.perf_counter()
    merge_bands(night, 40)
    assert _t.perf_counter() - t0 < 0.1
