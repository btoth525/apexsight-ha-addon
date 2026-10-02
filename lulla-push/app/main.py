import logging
"""Lulla Push + Sync relay — FastAPI app.

Self-hosted household sync (plan §2.3 Path D, docs/DECISIONS.md D-003) + the APNs push
relay (§7, mirrors apexsight-push). This file wires the **sync** surface; push endpoints
land in Phase 6.5 alongside the shared APNs .p8.

Public API (called by the iOS app):
  POST /v1/register     — a phone joins a household with the pairing code → bearer token
  POST /v1/sync/push    — push locally-changed records (dedupe + LWW on the server)
  GET  /v1/sync/pull    — pull everything since the device's cursor
  GET  /v1/sync/state   — counts (also feeds the admin dashboard)
  GET  /healthz         — liveness
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import apns, config, db, home, owlet_log, routing, security, sleep_history

app = FastAPI(title="Lulla Push + Sync Relay", docs_url=None, redoc_url=None)

# Public-internet auth hardening (this relay is reachable via the Cloudflare Tunnel):
# slow brute force against the pairing code well below its 36^8 keyspace, and never leak
# it via timing. Exempt only the test harness's ACCEPT_ANY_PAIRING mode, which registers
# many households per run and is never reachable outside `swift test`.
_register_limiter = security.RateLimiter(max_attempts=10, window_seconds=300)
# Unauthenticated partner pushes from build-41 phones (see _caller_household): a phone sends a
# handful per feed/diaper, so 30 a minute per client is generous and stops a flood.
_legacy_limiter = security.RateLimiter(max_attempts=30, window_seconds=60)
log = logging.getLogger("lulla")
_admin_limiter = security.RateLimiter(max_attempts=5, window_seconds=300)


def _client_key(request: Request) -> str:
    # Cloudflare's real-client-IP header is only trusted from a private peer (the tunnel runs on
    # the LAN); anyone hitting :6969 directly could otherwise pick a fresh "IP" per attempt.
    peer = request.client.host if request.client else "unknown"
    cf = request.headers.get("cf-connecting-ip")
    return cf if cf and security.is_private(peer) else peer


def household_id() -> str:
    """The household all data lives under. It used to BE the pairing code, so changing the code
    orphaned every record. Now it is pinned once (to the household the existing records use)
    and the pairing code is only the join secret, which can be rotated freely."""
    hid = db.get_config("household_id")
    if not hid:
        hid = db.main_household() or config.PAIRING_CODE
        db.set_config("household_id", hid)
    return hid


@app.on_event("startup")
async def _startup() -> None:
    db.init()
    household_id()                 # pin the household before the pairing code can change
    db.prune_deliveries(30)        # the delivery log only needs recent history
    # Watch the Owlet sock and auto-log sleep sessions (single-writer → both phones get one
    # shared entry). Only when we actually have HA access; wrapped so it can never crash the app.
    if home.SUPERVISOR_TOKEN:
        asyncio.create_task(_owlet_sleep_poller())
        asyncio.create_task(_backfill_sleep_segments())


async def _backfill_sleep_segments() -> None:
    """Seed the sleep history ONCE from HA's recorder so the chart doesn't launch empty.

    The recorder holds ~10 days; from here on the poller writes one `sleep_minute` row a minute
    and we keep them indefinitely (Owlet keeps session history forever — a chart that goes blank
    a fortnight back would be a step DOWN from what Taylor has today). The per-minute seed below
    is what the chart reads. The debounced `sleep_segments` replay is write-only (no endpoint
    reads that table); it's replayed through the poller's own debounce so the audit trail has no
    seam ten days back."""
    if db.get_config("owlet_backfill_done"):
        return
    try:
        readings = await home.sleep_state_history(days=10)
        if not readings:
            return          # sock never worn / recorder empty — try again next boot
        for seg in sleep_history.segments_from_readings(readings, until=time.time()):
            db.add_sleep_segment(seg.band, seg.start, seg.end)
        # Seed the per-minute timeline too (Owlet-matched stats): one row per minute, carrying the
        # last change forward (HA history is change-only).
        for i, (ts, state) in enumerate(readings):
            end = readings[i + 1][0] if i + 1 < len(readings) else time.time()
            m = int(ts // 60) * 60
            while m < end:
                db.set_sleep_minute(m, sleep_history._minute_state(state))
                m += 60
        db.set_config("owlet_backfill_done", owlet_log.now_iso())
    except Exception:
        pass                # never let a backfill take the relay down


# Poll fast enough that "she just woke up" reaches a parent in seconds, not a minute. The HA
# Core API call is local and cheap, so 15s is comfortable for a household relay.
_OWLET_POLL_SECONDS = 15

# The relay must be unable to reach Home Assistant for this long before it says "monitoring
# offline" — long enough to ride out a transient blip or a restart, short enough to matter.
HA_OUTAGE_SECONDS = 5 * 60


async def _owlet_sleep_poller() -> None:
    """Poll HA, run the pure sleep state machine, and write a synced sleep LogEvent when the
    baby wakes. The relay is the sole writer, so no per-phone duplicates. Best-effort forever."""
    tz = "UTC"
    try:
        cfg = await home._get("/config")
        if cfg and cfg.get("time_zone"):
            tz = cfg["time_zone"]        # render sleep times in the household's local zone
    except Exception:
        pass
    household = household_id()
    while True:
        try:
            if tz == "UTC":
                # Booting before HA is up used to stamp every sleep "UTC" until the next restart.
                try:
                    cfg = await home._get("/config")
                    if cfg and cfg.get("time_zone"):
                        tz = cfg["time_zone"]
                except Exception:
                    pass
            st = await home.state()
            vitals = st.get("vitals") or {}
            alerts = st.get("alerts") or {}
            baby = st.get("baby_name") or "Ryleigh"
            if not st.get("connected"):
                # Home Assistant unreachable (restart/update): we can't SEE the sock, which is not
                # the same as "no signal". Treating it as nosignal ended the night's sleep log and
                # the Live Activity and wiped the alert baseline (re-firing alerts on recovery).
                # Only the outage watchdog runs; everything else waits for HA to come back.
                await _watch_ha_link(False, baby, time.time())
                await asyncio.sleep(_OWLET_POLL_SECONDS)
                continue

            # 1) Relay Owlet's OWN alert flags — push once per OFF→ON episode. On the FIRST poll
            #    ever (no stored baseline) we seed silently, so a flag that's already on at deploy
            #    time (e.g. sock_off while it's charging) doesn't fire a spurious alert.
            raw_prev = db.get_config("owlet_alerts")
            if raw_prev is not None:
                last_real = db.get_config("owlet_last_real_cls")
                # The confirmed class lags reality by the 5-minute hold. A real class still being
                # timed (she JUST fell asleep, or JUST woke) is the better guide: the sock slipping
                # off 3 minutes after she dozed off is an alarm, and a parent pulling it off 3
                # minutes after she woke is not.
                pending = owlet_log.Debounced.from_json(db.get_config("owlet_sleep_cls")).candidate
                if pending in ("asleep", "awake"):
                    last_real = pending
                for key in owlet_log.alert_transitions(json.loads(raw_prev), alerts):
                    # "The sock came off" is an ALARM only if it came off while she was ASLEEP
                    # (unmonitored during sleep). When she's awake, a parent removed it on purpose
                    # (a feed, or putting it on the base to charge) — not an emergency. Suppressing
                    # those stops charging the sock from firing a Focus-piercing "sock came off".
                    if key in ("sock_off", "sock_disconnected") and last_real != "asleep":
                        continue
                    await _push_owlet_alert(key, baby)
            db.set_config("owlet_alerts", json.dumps(alerts))

            now_ts = time.time()

            # 0) RESTART / GAP GUARD. The relay persists its debounce candidates and the open
            #    debounced band (`sleep_segments`) across a restart. Without this, a deploy or
            #    reboot mid-sleep would
            #    (a) bridge the open band straight across the downtime, hiding any waking that
            #    happened while we were down, and (b) let a stale debounce candidate whose `since`
            #    predates the gap instant-confirm a Focus-piercing wake push from a single sample.
            #    On a detected gap we close the open band at the last poll and drop in-flight
            #    candidates so nothing confirms on stale time.
            last_poll = float(db.get_config("owlet_last_poll_ts") or 0)
            gap = now_ts - last_poll if last_poll else 0
            if last_poll and gap > _OWLET_POLL_SECONDS * 4:      # missed ~1 minute of polls
                open_band = db.get_config("owlet_band")
                open_band_start = float(db.get_config("owlet_band_start") or 0)
                if open_band and open_band_start:
                    db.add_sleep_segment(open_band, open_band_start, last_poll)
                db.set_config("owlet_band", "")
                db.set_config("owlet_band_start", "")
                for k in ("owlet_stage", "owlet_sleep_cls"):
                    d = owlet_log.Debounced.from_json(db.get_config(k))
                    db.set_config(k, owlet_log.Debounced(confirmed=d.confirmed).to_json())
            db.set_config("owlet_last_poll_ts", str(now_ts))

            # 0b) MONITORING WATCHDOG (the supervised loop, done safely). Fire ONLY when the relay
            #     genuinely loses the Home Assistant link — it can't read the sock at all — for a
            #     sustained window. That is "we've gone blind", a real problem worth knowing about.
            #     It is deliberately NARROW: it does NOT fire when the sock is merely on the base
            #     (HA reachable, just no Owlet reading — normal), nor on the app's heartbeat (a
            #     sleeping phone stops heart-beating overnight, which is also normal), nor as a
            #     CRITICAL alert (no entitlement, and the Owlet base station stays the real alarm).
            #     Edge-triggered: one "offline" note per outage, one "back" note on recovery.
            await _watch_ha_link(True, baby, now_ts)

            # 2) Sleep STAGE, debounced. The sock's raw stage flaps (deep-sleep runs have a
            #    median length of ~2 minutes), so acting on the raw edge produced notes that
            #    were already wrong by the time a phone lit up. Only a stage that HOLDS is
            #    announced. Nothing is announced while she's awake — (3) owns that.
            #
            #    Only a real SLEEP stage (light/deep) feeds this and the live headline. A raw
            #    "awake" while she's confirmed-asleep is a STIR, not a wake — `sleep_class("awake")`
            #    is "awake", not "nosignal", so the old `!= "nosignal"` filter let it through and
            #    flipped the Live Activity / widget headline to "Awake" mid-nap. Gate on
            #    `== "asleep"` so a stir is dropped here exactly as `band_for` drops it for the chart.
            raw_stage = vitals.get("sleep_state")
            stage_reading = (raw_stage if raw_stage
                             and owlet_log.sleep_class(raw_stage) == "asleep" else None)
            stage_state, new_stage = owlet_log.debounce(
                owlet_log.Debounced.from_json(db.get_config("owlet_stage")),
                stage_reading, now_ts, owlet_log.STAGE_HOLD_SECONDS)
            db.set_config("owlet_stage", stage_state.to_json())

            # 3) Sleep CLASS, debounced. One filter now drives BOTH the wake/asleep alert and
            #    the auto sleep log, so the two can never disagree — and the ~60-120s "awake"
            #    twitches that produced 140 pushes a day (and chopped one night into eleven
            #    "sleeps") are swallowed before either can act on them.
            raw_cls = owlet_log.sleep_class_from_alerts(alerts, raw_stage)
            cls_state, new_cls = owlet_log.debounce(
                owlet_log.Debounced.from_json(db.get_config("owlet_sleep_cls")),
                raw_cls, now_ts, owlet_log.WAKE_HOLD_SECONDS)
            db.set_config("owlet_sleep_cls", cls_state.to_json())
            cur = cls_state.confirmed or raw_cls

            # 3a) AWAKE <-> ASLEEP, on the CONFIRMED edge only, and ONLY across a real sleep↔wake
            #     transition. We compare against the last REAL class (awake/asleep), IGNORING any
            #     nosignal in between. That matters both ways:
            #       * nosignal→awake (sock put back on an already-awake baby) must NOT fire a
            #         Focus-piercing "She's waking up",
            #       * asleep→nosignal→awake (she wakes and they pull the sock to feed) MUST still
            #         fire it — `sock_off` maps to nosignal and is evaluated before the awake flag,
            #         so the immediately-previous confirmed class can be nosignal at the real wake.
            #     Tracking the last real class (not the immediate previous) gets both right.
            prev_real = db.get_config("owlet_last_real_cls") or None
            if prev_real is None and cur in ("awake", "asleep"):
                db.set_config("owlet_last_real_cls", cur)   # seed silently on first run / deploy
                prev_real = cur
            transition = owlet_log.wake_transition(prev_real, new_cls)
            if transition == "wake":
                # A confirmed WAKE ends the settle: clear any one-shot "tell me at deep sleep" arm
                # so it can't fire for a later, unrelated sleep the parent didn't ask about. Only
                # on a REAL sleep→wake edge: a nosignal→awake edge (sock back on after a feed,
                # she reads awake for five minutes, then drifts off) used to clear an arm the
                # parent had just set — while the app kept saying "You'll be alerted".
                db.set_config("owlet_deep_arm_until", "")
                if now_ts - float(db.get_config("owlet_awake_alert_ts") or 0) >= owlet_log.WAKE_ALERT_MIN_GAP:
                    await _push_wake_state(True, baby)
                    db.set_config("owlet_awake_alert_ts", str(now_ts))
            elif transition == "asleep":
                if now_ts - float(db.get_config("owlet_asleep_alert_ts") or 0) >= owlet_log.ASLEEP_ALERT_MIN_GAP:
                    await _push_wake_state(False, baby)
                    db.set_config("owlet_asleep_alert_ts", str(now_ts))
            # Remember the last REAL (awake/asleep) confirmed class for the next transition; a
            # nosignal (sock on the base) never overwrites it, so sock-off/on can't fake an edge.
            if new_cls in ("awake", "asleep"):
                db.set_config("owlet_last_real_cls", new_cls)

            # 3b) A confirmed stage note, rate-limited PER STAGE (1.3.0 shared one 10-minute
            #     budget across every stage, so a light-sleep ping silently swallowed the
            #     deep-sleep one). Only while she is confirmed asleep.
            if new_stage in owlet_log.ALERTING_STAGES and cur == "asleep":
                last_alerts = json.loads(db.get_config("owlet_stage_ts_by_stage") or "{}")
                if owlet_log.stage_alert_due(last_alerts, new_stage, now_ts):
                    await _push_sleep_stage(new_stage, baby)
                    last_alerts[new_stage] = now_ts
                    db.set_config("owlet_stage_ts_by_stage", json.dumps(last_alerts))

            # 3c) Silent nudge on any confirmed change so the phones' widgets / Lock Screen
            #     redraw without waiting on WidgetKit's refresh budget. Carries the reading
            #     itself, so the app can stamp its App Group snapshot with no round trip.
            # Stamp tonight's sleep anchor on the falling-asleep EDGE before anything reads it:
            # the refresh push right below carries it as `asleep_since`, and it used to be set
            # only further down (3c-ii), so the edge push went out with LAST night's anchor.
            # Back-stamped to when she actually fell asleep, like the auto-log.
            if new_cls == "asleep":
                db.set_config("owlet_activity_start", owlet_log.iso_at(
                    now_ts - owlet_log.WAKE_HOLD_SECONDS))
            if new_stage or new_cls:
                await _push_owlet_refresh(vitals, stage=stage_state.confirmed, sleep_class=cur)
            elif cur != "nosignal" and now_ts - float(db.get_config("owlet_refresh_push_ts") or 0) >= OWLET_REFRESH_HEARTBEAT_S:
                # Heartbeat: a 35-minute light-sleep run sent no push at all, so the widgets'
                # 15-minute staleness rule read "No reading" for ~3 h of every quiet night.
                await _push_owlet_refresh(vitals, stage=stage_state.confirmed, sleep_class=cur)

            # 3c-ii) The sleep Live Activity — the surface that actually answers "is she in deep
            #        sleep RIGHT NOW", which is the whole ask: a mom holding the baby after a feed,
            #        waiting to know it's safe to put her down. Pushed, so it costs no WidgetKit
            #        refresh budget and can't go stale between updates.
            #
            #        TWO stage signals feed it, on purpose:
            #          * the CONFIRMED stage (debounced) drives the stable "asleep for 2h" clock
            #            and the lifecycle (start on asleep, end on wake), and
            #          * the RAW stage drives the live headline, updated the instant the sock
            #            changes (~15-30s) rather than after a 2-minute confirm. The raw path only
            #            runs while she is CONFIRMED asleep, so it can never re-introduce the
            #            awake<->light flap storm the debounce exists to kill.
            #        The live headline holds the last real SLEEP stage. `stage_reading` is None
            #        during a raw "awake" stir (sanitized above) or nosignal, and on those ticks we
            #        must NOT flip the card to "Awake" — she's still confirmed-asleep, so keep
            #        showing "Deep sleep · 5 min". Only a real light/deep reading moves it.
            prev_live = db.get_config("owlet_live_stage") or ""
            live_changed = bool(stage_reading and stage_reading != prev_live)
            if live_changed:
                db.set_config("owlet_live_stage", stage_reading)
                db.set_config("owlet_live_stage_since", owlet_log.now_iso())
            live_stage = db.get_config("owlet_live_stage") or None
            live_since = db.get_config("owlet_live_stage_since") or owlet_log.now_iso()

            def _content(stage_confirmed):
                return _sleep_content_state(
                    sleep_started=db.get_config("owlet_activity_start") or owlet_log.now_iso(),
                    stage_since=db.get_config("owlet_activity_stage_since") or owlet_log.now_iso(),
                    stage=stage_confirmed, vitals=vitals,
                    live_stage=live_stage,
                    live_stage_since=live_since if live_stage else None)

            if new_cls == "asleep":
                # (`owlet_activity_start` was stamped above, before the refresh push.)
                db.set_config("owlet_activity_stage_since", owlet_log.now_iso())
                # Only push-to-start if the APP hasn't already started (and registered) one. The
                # app local-starts the card whenever it's open and she's asleep; a relay start on
                # top of that would stack a duplicate card on the Lock Screen. This covers the
                # app-suspended case; the app covers the app-open case. Both use the CONFIRMED
                # class, so they never disagree.
                if not db.activities_by_kind(OWLET_ACTIVITY_KIND):
                    # Stamp the self-heal clock too, so a phone that's slow to register this card
                    # doesn't get a SECOND start from the self-heal branch 15 seconds later.
                    db.set_config("owlet_activity_retry_ts", str(now_ts))
                    await _sleep_activity_start(baby=baby, state=_content(stage_state.confirmed))
            elif new_cls in ("awake", "nosignal"):
                # End on wake OR sock-off. The old code ended only on "awake", so removing the
                # sock (nosignal) left an orphaned card counting up forever while the sleep log
                # had already closed — and the next sleep stacked a second card on top. Clearing
                # the live stage here keeps the next session from inheriting a stale headline.
                # Clear the live stage BEFORE building the final state (the ended card used to keep
                # reading "Light Sleep"), and dismiss it now — without a dismissal date iOS leaves an
                # ended card on the Lock Screen for up to 4 hours.
                db.set_config("owlet_live_stage", "")
                live_stage = None
                await _sleep_activity_push("end", _content(None), dismissal_date=int(now_ts))
            elif new_stage and cur == "asleep":
                db.set_config("owlet_activity_stage_since", owlet_log.iso_at(
                    now_ts - owlet_log.STAGE_HOLD_SECONDS))
                await _sleep_activity_push("update", _content(new_stage))
            elif cur == "asleep" and not db.activities_by_kind(OWLET_ACTIVITY_KIND):
                # SELF-HEAL. The lifecycle above only fires on the falling-asleep EDGE, so a
                # phone that installs (or reinstalls, or is rebooted) mid-nap would sit with no
                # card until the next time she went down — "I installed it and nothing happened".
                # If she's asleep and nothing is running, open one.
                #
                # Rate-limited because the loop can't tell "no phone has a push-to-start token"
                # from "the start push hasn't been answered yet": without the guard, a phone that
                # never registers back would make us fire a start every 15 seconds forever.
                if now_ts - float(db.get_config("owlet_activity_retry_ts") or 0) >= 600:
                    db.set_config("owlet_activity_retry_ts", str(now_ts))
                    db.set_config("owlet_activity_start",
                                  db.get_config("owlet_activity_start") or owlet_log.now_iso())
                    db.set_config("owlet_activity_stage_since",
                                  db.get_config("owlet_activity_stage_since") or owlet_log.now_iso())
                    await _sleep_activity_start(baby=baby, state=_content(stage_state.confirmed))
            elif (live_changed and cur == "asleep"
                  and db.activities_by_kind(OWLET_ACTIVITY_KIND)):
                # RAW fast path. The confirmed-stage branch above didn't fire (no confirmed
                # change this tick), but the raw SLEEP stage moved — push it so the Live Activity
                # headline flips to "Deep sleep" within a poll, not after a 2-minute confirm.
                # `live_changed` is only ever a real light/deep transition (a stir is sanitized to
                # None upstream), so this can never push an "Awake" flap. No extra rate limit: the
                # 15s poll IS the floor, and a liveactivity push isn't iOS-budgeted.
                await _sleep_activity_push("update", _content(stage_state.confirmed))

            # 3c-ii-arm) One-shot "tell me the moment she's in deep sleep" — the transfer-window
            #            alert Owlet has no equivalent of. Fires on the RAW deep entry (waiting for
            #            a confirm would defeat the point), pierces Focus (time-sensitive: this is
            #            the one alert where that's unambiguously right), disarms after one shot,
            #            and self-expires so a forgotten arm can't ping at 4am.
            arm_until = float(db.get_config("owlet_deep_arm_until") or 0)
            decision = owlet_log.deep_arm_decision(
                armed_until=arm_until, now=now_ts, prev_stage=(prev_live or None),
                cur_stage=live_stage, sleep_class_confirmed=cur)
            if decision == "fire":
                db.set_config("owlet_deep_arm_until", "")     # one shot
                await _push_deep_sleep_reached(baby)
            elif decision == "expire":
                db.set_config("owlet_deep_arm_until", "")

            # 3c-iii) Record the sleep timeline.
            # Per-MINUTE raw timeline — the ONLY source the chart reads: `/v1/home/sleep/sessions`
            # (hypnogram + session card) and the owlet.refresh widget summary are both built from
            # it by `sleep_history.owlet_sessions`. Store the RAW sock state so 1-minute stats
            # match Owlet.
            db.set_sleep_minute(int(now_ts // 60) * 60,
                                sleep_history._minute_state(raw_stage))

            # The DEBOUNCED band (`sleep_segments`), written off the same confirmed signals as the
            # alerts and the auto-log. Nothing reads this table any more (the chart moved to the
            # per-minute timeline above); it's kept as an audit trail of the debounced signal.
            band = sleep_history.band_for(cur, stage_state.confirmed)
            # Back-stamp a band boundary caused by a CONFIRMED edge to when the change actually
            # started (now - hold), exactly as the auto sleep-log does, so the recorded bands
            # agree with the log on bedtime/wake. A class edge uses WAKE_HOLD; a
            # pure stage edge uses STAGE_HOLD; a plain fresh-signal tick uses now.
            if new_cls in ("awake", "asleep", "nosignal"):
                edge_ts = now_ts - owlet_log.WAKE_HOLD_SECONDS
            elif new_stage:
                edge_ts = now_ts - owlet_log.STAGE_HOLD_SECONDS
            else:
                edge_ts = now_ts
            open_band = db.get_config("owlet_band")
            open_band_start = float(db.get_config("owlet_band_start") or 0)
            if band != open_band:
                # Never let a back-stamp run the boundary earlier than the open band's own start.
                boundary = max(edge_ts, open_band_start) if open_band_start else edge_ts
                if open_band and open_band_start:
                    db.add_sleep_segment(open_band, open_band_start, boundary)
                db.set_config("owlet_band", band or "")
                db.set_config("owlet_band_start", str(boundary) if band else "")
            elif band and open_band_start:
                # Keep the OPEN band's end fresh so the stored band reaches "now" instead of
                # stopping at the last transition.
                db.add_sleep_segment(band, open_band_start, now_ts)

            # 3d) Auto-log sleep off the CONFIRMED class. The edge is back-stamped to when the
            #     change actually started (now - hold) rather than when we believed it, so a
            #     debounced log still records the true times.
            open_start = db.get_config("owlet_open_start") or None
            now = (owlet_log.iso_at(now_ts - owlet_log.WAKE_HOLD_SECONDS) if new_cls
                   else owlet_log.now_iso())
            decision = owlet_log.decide(cur, open_start, now)
            db.set_config("owlet_open_start", decision.new_open_start or "")
            if decision.write:
                payload = owlet_log.build_sleep_payload(
                    start_iso=decision.write["start"], end_iso=decision.write["end"],
                    tz=tz, now_iso=now)
                db.upsert(household, "LogEvent", payload["id"], time.time(),
                          "owlet", False, json.dumps(payload))
        except Exception:
            # A bad poll must never take the relay down — but say what broke.
            log.exception("owlet poll failed")
        await asyncio.sleep(_OWLET_POLL_SECONDS)


async def _watch_ha_link(connected: bool, baby: str, now_ts: float) -> None:
    """Edge-triggered HA-outage note: one "offline" per outage, one "back" on recovery."""
    if connected:
        if db.get_config("owlet_ha_out_fired"):
            await _push_monitoring(baby, offline=False)   # recovered
        db.set_config("owlet_ha_out_since", "")
        db.set_config("owlet_ha_out_fired", "")
        return
    out_since = db.get_config("owlet_ha_out_since")
    if not out_since:
        db.set_config("owlet_ha_out_since", str(now_ts))
    elif (not db.get_config("owlet_ha_out_fired")
          and now_ts - float(out_since) >= HA_OUTAGE_SECONDS):
        await _push_monitoring(baby, offline=True)
        db.set_config("owlet_ha_out_fired", "1")


async def _push_wake_state(awake: bool, baby: str) -> None:
    """She just woke up / just fell asleep. Waking is time-sensitive (that's the one you want to
    catch through Focus — a feed usually follows); falling asleep is a quiet note."""
    await _push_internal(PushEventBody(
        event="owlet.awake" if awake else "owlet.asleep",
        household=household_id(),
        title=f"{'👀' if awake else '😴'} {baby}",
        body="She's waking up." if awake else "She's fallen asleep.",
        interruption_level="time-sensitive" if awake else "passive",
        collapse_id="owlet-wake",
    ))


OWLET_ACTIVITY_KIND = "owletSleep"

# How long a one-shot "tell me at deep sleep" arm stays live before it self-expires. Long
# enough to cover settling after a feed, short enough that a forgotten arm never pings overnight.
DEEP_ARM_WINDOW_SECONDS = 3600.0


def _sleep_content_state(*, sleep_started: str, stage_since: str, stage: Optional[str],
                         vitals: dict, live_stage: Optional[str] = None,
                         live_stage_since: Optional[str] = None) -> dict:
    """The Live Activity's `content-state`. Must round-trip EXACTLY with Swift's
    `OwletSleepContentState` — same keys, same types, ISO-8601 dates (the app's decoder uses
    `.iso8601`). Getting this wrong doesn't error; the activity just silently stops updating.

    Two stage pairs, on purpose (this is the transfer-window feature):
      * `stageLabel`/`stageSince` — the CONFIRMED stage (debounced). Stable; never resets on a
        flicker. Kept for anything that wants a trustworthy stage.
      * `liveStage`/`liveStageSince` — the RAW stage, straight off the sock. This is what a mom
        holding the baby after a feed is staring at: it flips to "Deep sleep" the instant the sock
        says so, ~15-30s, not after a 2-minute confirm. The app shows this as the live headline
        and the confirmed pair for the trustworthy duration. Defaults to the confirmed values so
        an older relay payload still renders."""
    return {
        "sleepStartedAt": sleep_started,
        "stageSince": stage_since,
        "stageLabel": owlet_log.stage_label(stage) if stage else None,
        "liveStage": owlet_log.stage_label(live_stage) if live_stage else None,
        "liveStageSince": live_stage_since,
        "bpm": vitals.get("bpm"),
        "spo2": vitals.get("spo2"),
    }


async def _sleep_activity_start(*, baby: str, state: dict) -> None:
    """Push-to-start the sleep Live Activity on both phones. Needs a push-to-start token, which
    the app registers once it observes one; devices without one are simply skipped."""
    client = apns.get_client()
    if not client.is_configured():
        return
    payload = apns.build_liveactivity_payload(
        event="start", content_state=state,
        attributes_type="OwletSleepAttributes", attributes={"childName": baby},
    )
    for dev in db.push_devices(household_id()):
        if not dev["push_to_start_token"]:
            continue
        try:
            await _send_and_log(client, "activity.start", dev["push_to_start_token"],
                                dev["env"], payload, push_type="liveactivity")
        except Exception:
            pass


async def _sleep_activity_push(event: str, state: dict, dismissal_date: Optional[int] = None) -> None:
    """Update (or end) every running sleep activity. On `end` the registry row goes too, so a
    stale token can't keep a dead activity alive on the Lock Screen."""
    client = apns.get_client()
    if not client.is_configured():
        return
    acts = db.activities_by_kind(OWLET_ACTIVITY_KIND)
    if not acts:
        return
    payload = apns.build_liveactivity_payload(event=event, content_state=state,
                                              dismissal_date=dismissal_date)
    for act in acts:
        try:
            await _send_and_log(client, f"activity.{event}", act["push_token"], act["env"],
                                payload, push_type="liveactivity")
        except Exception:
            pass
        if event == "end":
            db.delete_activity(act["activity_id"])


# Keep the encoded owlet.refresh under this, comfortably inside APNs' hard 4096-byte limit (a
# payload over the limit is rejected WHOLE — the widget then stops updating near wake-up, which
# is exactly when a busy night has the most bands).
REFRESH_PAYLOAD_BUDGET = 3900
# The App Group summary never needs more than this many bands, whatever the byte budget allows.
SUMMARY_MAX_BANDS = 160
_BAND_ROW = {"deep_sleep": 0, "light_sleep": 1, "awake": 2}


def _newest_sleep_session(now: float) -> Optional[sleep_history.OwletSession]:
    """The newest Owlet-matched session (the one the home-screen Sleep widget shows), or None."""
    minutes = [(r["minute_ts"], r["state"]) for r in db.sleep_minutes(now - 2 * 86400)]
    sessions = sleep_history.owlet_sessions(minutes)
    return max(sessions, key=lambda x: x.start) if sessions else None


def _sleep_summary_dict(ss: sleep_history.OwletSession, bands: list, now: float) -> dict:
    """The COMPACT widget summary: totals + barcode bands (kind 0=deep,1=light,2=awake; start/end
    seconds from the session start). Sock-off time inside the night is a hole between bands."""
    return {
        "start": owlet_log.iso_at(ss.start), "end": owlet_log.iso_at(ss.end),
        "asleep_seconds": ss.asleep_minutes * 60, "awake_seconds": ss.awake_minutes * 60,
        "light_seconds": ss.light_minutes * 60, "deep_seconds": ss.deep_minutes * 60,
        "wakings": ss.wakings,
        "in_progress": (now - ss.end) < 900,     # still her current sleep
        "bands": [{"kind": _BAND_ROW.get(b.band, 2),
                   "start": round(b.start - ss.start), "end": round(b.end - ss.start)}
                  for b in bands],
    }


def build_owlet_refresh_payload(owlet: dict, session: Optional[sleep_history.OwletSession], *,
                                now: float, budget: int = REFRESH_PAYLOAD_BUDGET,
                                max_bands: int = SUMMARY_MAX_BANDS) -> dict:
    """The `owlet.refresh` background payload, guaranteed to fit `budget` bytes on the wire.

    1. The night's bands, merged (never skipped) down to `max_bands`.
    2. Still too big → merge further, to as many bands as the remaining bytes can hold.
    3. Still too big (merging can't hide a real waking, so a pathological night may not shrink
       enough) → drop the `sleep` key. The owlet reading ALWAYS goes through: the app keeps its
       previous sleep summary when a refresh carries none, so a stale widget beats a dead push.
    """
    data: dict = {"event": "owlet.refresh", "owlet": owlet}
    if session is None:
        return apns.build_background_payload(data=data)
    bands = sleep_history.merge_bands(session.segments, max_bands)
    data["sleep"] = _sleep_summary_dict(session, bands, now)
    payload = apns.build_background_payload(data=data)
    if apns.payload_size(payload) <= budget:
        return payload
    # Room for bands = budget minus everything else; each band costs at most this many bytes
    # (offsets can't exceed the night's span), so this cap is a guaranteed fit if merging can
    # reach it.
    data["sleep"] = _sleep_summary_dict(session, [], now)
    room = budget - apns.payload_size(apns.build_background_payload(data=data))
    digits = len(str(int(round(session.end - session.start))))
    per_band = len('{"kind":0,"start":,"end":},') + 2 * digits
    cap = min(room // per_band, max_bands)
    if cap >= 1:
        data["sleep"] = _sleep_summary_dict(
            session, sleep_history.merge_bands(session.segments, cap), now)
        payload = apns.build_background_payload(data=data)
        if apns.payload_size(payload) <= budget:
            return payload
    data.pop("sleep", None)
    log.warning("owlet.refresh: sleep summary dropped to fit the APNs payload limit")
    return apns.build_background_payload(data=data)


def _asleep_since(sleep_class: Optional[str]) -> Optional[str]:
    """When she fell asleep THIS time (the relay's own Live Activity anchor, back-stamped to the
    real edge), or None when she isn't confirmed asleep. Not the newest session's start: that
    can be last night's (a session only appears after ~10 min asleep) or, now that a sock-off
    gap no longer splits a night, the start of the whole night rather than this stretch."""
    if sleep_class != "asleep":
        return None
    return db.get_config("owlet_activity_start") or None


async def _push_owlet_refresh(vitals: dict, *, stage: Optional[str],
                              sleep_class: str) -> None:
    """A silent (content-available) nudge carrying the current reading, so both phones can stamp
    their App Group snapshot and redraw the widget / Lock Screen immediately.

    Sent on a CONFIRMED change (a handful of times a day), plus a heartbeat at most every
    OWLET_REFRESH_HEARTBEAT_S while the sock is reporting (see the poller) — so the Lock Screen
    glance never ages past one heartbeat during a long quiet stage. iOS budgets background pushes
    at "two or three an hour", so a per-poll nudge would simply be dropped. No alert, no sound,
    no badge — this is the data path behind the glance, not a notification.

    Every send (change-driven or heartbeat) stamps `owlet_refresh_push_ts`, so the heartbeat
    only fires in genuinely quiet stretches."""
    db.set_config("owlet_refresh_push_ts", str(time.time()))
    client = apns.get_client()
    if not client.is_configured():
        return
    owlet = {
        "bpm": vitals.get("bpm"), "spo2": vitals.get("spo2"),
        "battery_pct": vitals.get("battery_pct"), "sock_on": vitals.get("sock_on"),
        "sleep_state": stage, "sleep_class": sleep_class,
        "asleep_since": _asleep_since(sleep_class),
        "read_at": owlet_log.now_iso(),
    }
    now = time.time()
    # The home-screen Sleep widget, refreshed in the background.
    payload = build_owlet_refresh_payload(owlet, _newest_sleep_session(now), now=now)
    for dev in db.push_devices(household_id()):
        try:
            await _send_and_log(client, "owlet.refresh", dev["device_token"], dev["env"],
                                payload, push_type="background", collapse_id="owlet-refresh")
        except Exception:
            pass   # a silent nudge is best-effort by definition


async def _push_monitoring(baby: str, *, offline: bool) -> None:
    """The relay lost (or regained) its link to Home Assistant — i.e. it can't read the sock at
    all. Active level, NOT critical: it's an FYI ("your monitor went offline, check it"), not an
    alarm — the Owlet base station remains the real alarm. Goes to both phones (no actor)."""
    await _push_internal(PushEventBody(
        event="monitoring.offline" if offline else "monitoring.back",
        household=household_id(),
        title="\u26A0\uFE0F Monitor offline" if offline else "\u2705 Monitor back",
        body=("Lulla can't reach the Owlet sock right now — check Home Assistant."
              if offline else f"Lulla can see {baby}'s sock again."),
        interruption_level="active" if offline else "passive",
        collapse_id="lulla-monitoring",
    ))


async def _push_deep_sleep_reached(baby: str) -> None:
    """She just reached deep sleep and a parent armed the one-shot alert — "safe to put her
    down". Time-sensitive so it pierces Sleep Focus; this is the rare case where waking the phone
    is exactly what was asked for."""
    await _push_internal(PushEventBody(
        event="owlet.deep_reached", household=household_id(),
        title=f"\U0001F634 {baby} is in deep sleep",
        body="Good window to put her down.",
        interruption_level="time-sensitive", collapse_id="owlet-deep-reached",
    ))


async def _push_sleep_stage(state: str, baby: str) -> None:
    """A quiet, collapsing note that the baby moved to a new sleep stage. Passive so it never
    buzzes overnight; it just appears for a glance."""
    await _push_internal(PushEventBody(
        event="owlet.sleep_stage", household=household_id(),
        title=f"😴 {baby}", body=f"Now: {owlet_log.stage_label(state)}",
        interruption_level="passive", collapse_id="owlet-stage",
    ))


async def _push_owlet_alert(key: str, baby: str) -> None:
    """Fan an Owlet alert flag out to both phones (relaying the sock's own flag, not a threshold
    we invented). Safety-critical flags are time-sensitive so they pierce Sleep Focus."""
    meta = owlet_log.ALERT_META.get(key)
    if not meta:
        return
    phrase, critical = meta
    await _push_internal(PushEventBody(
        event=f"owlet.{key}", household=household_id(),
        title=f"⚠️ {baby}", body=f"Owlet alert — {phrase}. Check the base station.",
        interruption_level="time-sensitive" if critical else "active",
        collapse_id=f"owlet-{key}",
    ))


class APNsConfigBody(BaseModel):
    pairing_code: str
    p8: str
    key_id: str
    team_id: str
    bundle_id: str
    env_mode: str = "auto"


@app.post("/v1/admin/apns")
async def set_apns_config(body: APNsConfigBody, request: Request):
    """One-shot APNs credential load (stand-in for the admin GUI). Pairing-code protected;
    the .p8 lands only in /data (db.config), never in the repo."""
    if not _admin_limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="too many attempts, try again later")
    if not security.safe_equals(body.pairing_code.upper().strip(), config.PAIRING_CODE):
        raise HTTPException(status_code=403, detail="pairing code mismatch")
    db.set_config("apns_p8", body.p8)
    db.set_config("apns_key_id", body.key_id)
    db.set_config("apns_team_id", body.team_id)
    db.set_config("apns_bundle_id", body.bundle_id)
    db.set_config("apns_env_mode", body.env_mode)
    return {"ok": True, "apns_configured": True}


class PruneBody(BaseModel):
    pairing_code: str


@app.post("/v1/admin/prune_devices")
async def prune_devices(body: PruneBody, request: Request):
    """Remove leftover debug/test sync devices (device_id like 'debug-%'). Pairing-protected.
    Real phones use UUID device_ids, so they can never be pruned by this."""
    if not _admin_limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="too many attempts, try again later")
    if not security.safe_equals(body.pairing_code.upper().strip(), config.PAIRING_CODE):
        raise HTTPException(status_code=403, detail="pairing code mismatch")
    return {"ok": True, "pruned": db.prune_test_devices()}


# ---- models -----------------------------------------------------------------

class RegisterBody(BaseModel):
    pairing_code: str
    device_id: str
    device_name: Optional[str] = None
    # Optional push registration (§7.2): when device_token is present, the phone is also
    # recorded in the push registry so it can receive APNs. env is tracked PER token.
    parent_id: Optional[str] = None
    device_token: Optional[str] = None
    push_env: Optional[str] = Field(default=None, alias="env")   # "prod" | "sandbox"
    # accept BOTH the field name the app sends ("env") and the canonical "push_env".
    push_to_start_token: Optional[str] = None
    app_version: Optional[str] = None


class SyncRecord(BaseModel):
    type: str
    id: str
    updated_at: float          # epoch seconds (Swift Date → timeIntervalSince1970)
    created_by: str = ""
    is_tombstoned: bool = False
    payload: str               # opaque JSON string (the LogEventSnapshot etc.)


class PushBody(BaseModel):
    records: list[SyncRecord] = Field(default_factory=list)


# ---- auth -------------------------------------------------------------------

def _household(authorization: Optional[str] = Header(default=None)) -> str:
    """Resolve the bearer token to a household. Every sync call is scoped to it."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    row = db.resolve_token(token)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return row["household"]


# ---- endpoints --------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "service": "lulla-push",
        "pairing_code_set": bool(config.PAIRING_CODE),
        "apns_configured": apns.get_client().is_configured(),
    }


@app.post("/v1/register")
async def register(body: RegisterBody, request: Request):
    code = body.pairing_code.upper().strip()
    if config.ACCEPT_ANY_PAIRING:
        household = code                       # TEST mode: pairing code IS the household
    else:
        if not security.safe_equals(code, config.PAIRING_CODE):
            # Charge the limiter only on a WRONG code. Behind the Cloudflare tunnel both phones
            # share one client key (the home's public IP), and the app registers 2-3 times per
            # cold launch — counting correct registrations tripped 429 in ordinary use and
            # left a phone token-less (no sync, no Owlet card, no camera) for the whole
            # session. Brute force is still throttled: 10 wrong guesses per 5 min.
            if not _register_limiter.allow(_client_key(request)):
                raise HTTPException(status_code=429, detail="too many attempts, try again later")
            raise HTTPException(status_code=403, detail="pairing code mismatch")
        household = household_id()
    token = db.register_device(household, body.device_id, body.device_name)
    if body.device_token:
        db.upsert_push_device(
            device_token=body.device_token,
            household=household,
            parent_id=body.parent_id or body.device_id,
            env=(body.push_env or "prod"),
            push_to_start_token=body.push_to_start_token,
            app_version=body.app_version,
        )
    return {"token": token, "household": household}


# Ryleigh's nursery camera, proxied from Frigate. The relay shares the LAN with Frigate, so it
# can fetch the snapshot and hand it back over the (token-authed, TLS-tunnelled) relay — which is
# how the phone sees the camera off-WiFi without exposing Frigate to the internet. Defaults are
# overridable via db config (frigate_url / frigate_camera) with no redeploy.
_FRIGATE_URL_DEFAULT = "http://192.168.1.204:5000"
_FRIGATE_CAMERA_DEFAULT = "Ryleighs_Rm"


@app.get("/v1/camera/snapshot")
async def camera_snapshot(household: str = Depends(_household)):
    base = (db.get_config("frigate_url") or _FRIGATE_URL_DEFAULT).rstrip("/")
    cam = db.get_config("frigate_camera") or _FRIGATE_CAMERA_DEFAULT
    url = f"{base}/api/{cam}/latest.jpg?h=720"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="camera unavailable")
        return Response(content=r.content, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="camera unreachable")


@app.get("/v1/home/history")
async def home_history(hours: int = 12, household: str = Depends(_household)):
    """Owlet vitals history for the app's trend charts (HR / O2 / skin temp)."""
    return await home.vitals_history(hours=max(1, min(hours, 72)))


class SettingsBody(BaseModel):
    feed_day_hours: float = Field(gt=0, le=24)
    feed_night_hours: float = Field(ge=0, le=24)     # 0 = overnight feeds "on demand" (app build 42+)
    night_start: int = Field(ge=0, le=23)
    night_end: int = Field(ge=0, le=23)
    updated_at: float          # client stamp; the newest write wins (LWW), like every record


@app.get("/v1/settings")
async def get_settings(household: str = Depends(_household)):
    """Household-shared settings (the feed schedule). Both phones read this so a change on one
    follows to the other."""
    raw = db.get_config("household_settings")
    return {"settings": json.loads(raw) if raw else None}


@app.post("/v1/settings")
async def set_settings(body: SettingsBody, household: str = Depends(_household)):
    raw = db.get_config("household_settings")
    if raw:
        cur = json.loads(raw)
        if float(cur.get("updated_at", 0)) > body.updated_at:
            return {"ok": True, "settings": cur}     # a newer change already won
    db.set_config("household_settings", json.dumps(body.model_dump()))
    return {"ok": True, "settings": body.model_dump()}


def _caller_device(authorization: Optional[str] = Header(default=None)) -> str:
    """The calling phone's device id (same token check as _household)."""
    _household(authorization)
    row = db.resolve_token(authorization.split(" ", 1)[1].strip())
    return row["device_id"] if row else ""


POKE_WINDOW_S = 30.0                       # iOS budgets background pushes
OWLET_REFRESH_HEARTBEAT_S = 1200.0         # ≤3 background pushes/hour incl. change-driven ones
_last_poke: dict[str, float] = {}
_pending_poke: dict[str, "asyncio.Task"] = {}
_pending_exclude: dict[str, Optional[str]] = {}


async def _send_poke(household: str, exclude_device: Optional[str]) -> None:
    _last_poke[household] = time.time()
    client = apns.get_client()
    if not client.is_configured():
        return
    payload = apns.build_background_payload(data={"event": "sync.refresh"})
    for dev in db.push_devices(household):
        if exclude_device and dev["parent_id"] == exclude_device:
            continue
        try:
            await _send_and_log(client, "sync.refresh", dev["device_token"], dev["env"], payload,
                                push_type="background")
        except Exception:
            log.exception("sync poke failed")


async def _poke_later(household: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await _send_poke(household, _pending_exclude.pop(household, None))
    finally:
        _pending_poke.pop(household, None)


async def _poke_partners(household: str, exclude_device: str) -> None:
    """Silently wake the OTHER phones so they pull this change now. Without it, a dose given on
    one phone left the other's "Tylenol due" reminder armed until that phone was next opened.

    At most one poke per household per window (iOS budgets background pushes) — but the LAST
    write inside a window is DEFERRED to the window's end, never dropped: a dose logged 20 s
    after a diaper used to lose its poke, leaving the partner's "Tylenol due" armed and its
    banner's double-dose guard blind until that phone was next opened."""
    elapsed = time.time() - _last_poke.get(household, 0)
    if elapsed >= POKE_WINDOW_S:
        await _send_poke(household, exclude_device)
        return
    if household in _pending_poke:
        if _pending_exclude.get(household) != exclude_device:
            _pending_exclude[household] = None     # both phones wrote: wake both
        return
    _pending_exclude[household] = exclude_device
    _pending_poke[household] = asyncio.create_task(
        _poke_later(household, max(0.0, POKE_WINDOW_S - elapsed)))


@app.post("/v1/sync/push")
async def sync_push(body: PushBody, household: str = Depends(_household),
                    device: str = Depends(_caller_device)):
    applied = 0
    max_seq = 0
    now = time.time()
    for r in body.records:
        # A record stamped in the future (bad clock, or a hostile client) would beat every later
        # real edit under last-write-wins. Cap it at "now".
        updated_at = min(r.updated_at, now + 300)
        res = db.upsert(household, r.type, r.id, updated_at, r.created_by,
                        r.is_tombstoned, r.payload)
        if res["applied"]:
            applied += 1
        max_seq = max(max_seq, res["server_seq"])
    if applied:
        asyncio.create_task(_poke_partners(household, device))
    return {"applied": applied, "received": len(body.records), "cursor": max_seq}


@app.get("/v1/sync/pull")
async def sync_pull(since: int = 0, limit: int = 500, household: str = Depends(_household)):
    return db.pull(household, since, limit)


@app.get("/v1/sync/state")
async def sync_state(household: str = Depends(_household)):
    return db.state(household)


# ---- the house (Phase 5, §6) — read straight from HA, zero app-side setup ---

class ToggleBody(BaseModel):
    entity_id: str


@app.get("/v1/home/state")
async def home_state(household: str = Depends(_household)):
    """Owlet vitals + the nursery strip, auto-discovered by entity name. `connected` tells
    the app whether this add-on could reach Home Assistant's own API at all — independent
    of whether any matching entities exist yet.

    Also carries the CONFIRMED (debounced) sleep state — the SAME signal that drives the
    notifications, the auto-log, the hypnogram, and the Live Activity lifecycle. The app uses
    this for every awake/asleep status word (Today hero, Owlet card, Sleep card, Live Activity)
    so no two surfaces can ever contradict each other. The raw `vitals.sleep_state` is still
    returned for the instant transfer-window ("she just hit deep sleep") only.
    """
    st = await home.state()
    cls = owlet_log.Debounced.from_json(db.get_config("owlet_sleep_cls")).confirmed
    stage = owlet_log.Debounced.from_json(db.get_config("owlet_stage")).confirmed
    st["sleep_class"] = cls                # "awake" | "asleep" | "nosignal" | None
    st["stage_confirmed"] = stage          # "light_sleep" | "deep_sleep" | None
    # When she fell asleep THIS time ("...Z" ISO, back-stamped to the real edge) — the anchor
    # for every "asleep for 2h" clock, including the Live Activity. null unless confirmed asleep.
    st["asleep_since"] = _asleep_since(cls)
    # Whether the one-shot deep-sleep alert is armed (same expression as deep_arm_status), so the
    # app's 10-second poll sees the relay fire/clear it instead of the button staying greyed out.
    until = float(db.get_config("owlet_deep_arm_until") or 0)
    st["deep_armed"] = bool(until and time.time() <= until)
    return st


@app.get("/v1/home/sleep/sessions")
async def home_sleep_sessions(days: int = 7, household: str = Depends(_household)):
    """Sleep sessions + their hypnogram bands, newest first — everything the app needs to draw
    the chart and the session card without re-deriving anything.

    A "session" is what a parent means by one sleep: brief wakings stay INSIDE it and are
    counted, the way Owlet defines a waking, rather than being split into separate naps.
    """
    days = max(1, min(int(days), 120))
    since = time.time() - days * 86400
    # Read one extra day back so a night that straddles `since` is assembled WHOLE, then drop it:
    # cutting the window mid-night used to return its tail as a short, wrong "session".
    rows = db.sleep_minutes(since - 86400)
    minutes = [(r["minute_ts"], r["state"]) for r in rows]
    sessions = [s for s in sleep_history.owlet_sessions(minutes) if s.start >= since]
    sessions.sort(key=lambda s: s.start, reverse=True)
    return {
        "days": days,
        "backfilled": bool(db.get_config("owlet_backfill_done")),
        "minute_count": sum(1 for r in rows if r["minute_ts"] >= since),
        "sessions": [s.as_dict() for s in sessions],
    }


@app.post("/v1/home/sleep/arm-deep")
async def arm_deep_sleep(household: str = Depends(_household)):
    """Arm the one-shot "tell me when she's in deep sleep" alert. Auto-expires so a forgotten arm
    can't fire hours later; if she's ALREADY in deep sleep the poller catches it on the next
    tick only if it's a fresh entry, so arming during deep sleep waits for the next entry (which
    is the honest behaviour — you want to know when she GOES deep, not that she is)."""
    import time as _time
    until = _time.time() + DEEP_ARM_WINDOW_SECONDS
    db.set_config("owlet_deep_arm_until", str(until))
    return {"armed": True, "expires_in_seconds": DEEP_ARM_WINDOW_SECONDS}


@app.get("/v1/home/sleep/arm-deep")
async def deep_arm_status(household: str = Depends(_household)):
    """Whether the one-shot deep-sleep alert is currently armed (for the app's button state)."""
    import time as _time
    until = float(db.get_config("owlet_deep_arm_until") or 0)
    armed = bool(until and _time.time() <= until)
    return {"armed": armed, "expires_at": until if armed else None}


@app.post("/v1/home/toggle")
async def home_toggle(body: ToggleBody, household: str = Depends(_household)):
    # Only the nursery strip's own switches/lights. This runs with the Supervisor's admin token,
    # so passing any entity through let a client open covers, run scripts or flip automations.
    st = await home.state()
    allowed = {n.get("entity_id") for n in st.get("nursery") or [] if n.get("is_toggle")}
    if body.entity_id not in allowed:
        raise HTTPException(status_code=403, detail="only the nursery toggles can be switched here")
    ok = await home.toggle(body.entity_id)
    return {"ok": ok}


# ---- push / eventing (Phase 6.5, plan §7) -----------------------------------

class ActivityRegisterBody(BaseModel):
    activity_id: str
    push_token: str
    kind: str = ""
    child_id: str = ""
    env: str = "prod"                      # per-token APNs env (sandbox/prod), §7.3


class PushEventBody(BaseModel):
    event: str
    child_id: str = ""
    title: str = ""
    body: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    exclude_parent_id: Optional[str] = None
    interruption_level: str = "active"     # active | passive | time-sensitive | critical
    collapse_id: str = ""
    category: str = ""
    household: Optional[str] = None
    child_asleep: bool = False             # caller (HA/app) reports the child's sleep state


class ActivityStartBody(BaseModel):
    child_id: str = ""
    kind: str = ""
    attributes_type: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    content_state: dict[str, Any] = Field(default_factory=dict)
    stale_date: Optional[int] = None
    exclude_parent_id: Optional[str] = None
    household: Optional[str] = None


class ActivityUpdateBody(BaseModel):
    activity_id: str = ""
    child_id: str = ""
    content_state: dict[str, Any] = Field(default_factory=dict)
    stale_date: Optional[int] = None


class ActivityEndBody(BaseModel):
    activity_id: str = ""
    child_id: str = ""
    content_state: dict[str, Any] = Field(default_factory=dict)
    dismissal_date: Optional[int] = None


class TestBody(BaseModel):
    title: str = "Lulla test"
    body: str = "This is a test notification."
    critical: bool = False
    household: Optional[str] = None


class HeartbeatBody(BaseModel):
    parent_id: Optional[str] = None
    device_id: Optional[str] = None
    ha_ok: bool = True                     # app relays whether HA looked reachable
    owlet_unavailable: bool = False


# Watchdog thresholds (seconds). The app should heartbeat well inside these.
HEARTBEAT_TIMEOUT = 15 * 60
HA_TIMEOUT = 15 * 60


def _now_local_minutes() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


async def _send_and_log(client, event, token, env, payload, *, push_type, collapse_id="",
                        topic_override=None):
    """Send one push, log it, and prune the token on 410 / dead-token. Returns detail."""
    ok, status, reason = await client.send_to_token(
        token, env, payload, push_type=push_type, collapse_id=collapse_id,
        topic_override=topic_override,
    )
    db.log_delivery(event, status, reason)
    # Feed the watchdog's push-channel signal (§7.7). A genuine TRANSPORT failure — network
    # error (status 0) or APNs 5xx — means the alert channel itself is down; a dead-token
    # 410 is routine housekeeping and must NOT be read as a chain break. A success clears it.
    if ok:
        db.set_monitoring("last_push_error", 0.0)
    elif status == 0 or status >= 500:
        db.set_monitoring("last_push_error", time.time())
    if not ok and apns.is_dead_token(status, reason):
        db.delete_push_device(token)
        return {"token": token, "ok": False, "status": status, "reason": reason, "pruned": True}
    return {"token": token, "ok": ok, "status": status, "reason": reason, "pruned": False}


def _caller_household(request: Request, authorization: Optional[str]) -> str:
    """Bearer token → household. Phones on LullaSight build 41 and earlier send no token on
    /v1/push and /v1/register/activity; those calls are still accepted (sanitized, rate-limited)
    ONLY while a registered phone still runs such a build. Once both phones report build 42+,
    an unauthenticated call is refused, with no config change needed."""
    if authorization:
        return _household(authorization)
    if not db.legacy_push_clients(min_build=42):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not _legacy_limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    return household_id()


@app.post("/v1/register/activity")
async def register_activity(body: ActivityRegisterBody, request: Request,
                            authorization: Optional[str] = Header(default=None)):
    _caller_household(request, authorization)
    db.register_activity(body.activity_id, body.child_id, body.kind, body.push_token, body.env)
    return {"status": "ok", "activity_id": body.activity_id}


# Keys a caller may never set inside `data`: they would replace the APNs header or drive the
# app's silent Owlet handler (fake vitals on the Lock Screen, ending the sleep Live Activity).
_RESERVED_DATA_KEYS = {"aps", "owlet", "sleep", "event"}


@app.post("/v1/push")
async def push(body: PushEventBody, request: Request,
               authorization: Optional[str] = Header(default=None)):
    """Fan an event out to push devices, applying §7.4 routing (non-negotiable):
    never notify exclude_parent_id, collapse by collapse_id, respect quiet hours except
    time-sensitive, and downgrade non-urgent to silent when nap_aware + child asleep."""
    body.household = _caller_household(request, authorization)
    body.data = {k: v for k, v in body.data.items()
                 if k not in _RESERVED_DATA_KEYS and isinstance(v, (str, int, float, bool))}
    if body.interruption_level == "critical":
        body.interruption_level = "time-sensitive"   # no critical entitlement; never let a caller fake one
    return await _fan_out(body)


async def _push_internal(body: PushEventBody) -> None:
    """The poller's way to fan an alert out (wake/asleep, Owlet flags, deep-sleep one-shot,
    stage notes, monitor offline/back). It bypasses the HTTP route on purpose: the route's
    signature carries `request` + `authorization` for the legacy limiter (1.10.0), and calling
    it directly raised TypeError — swallowed by a bare `except: pass`, so every one of these
    alerts was silently dead for two releases. Never crash the poller, but never hide a failure
    either: anything that goes wrong is in the add-on log."""
    if not apns.get_client().is_configured():
        log.warning("%s push skipped: APNs not configured", body.event)
        return
    try:
        await _fan_out(body)
    except Exception:
        log.exception("%s push failed", body.event)


async def _fan_out(body: PushEventBody) -> dict:
    """Route + deliver one already-authorized event to every push device in its household.
    Shared by the /v1/push route and the poller-side helpers (`_push_internal`)."""
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")

    in_quiet = routing.is_quiet_hours(
        _now_local_minutes(),
        routing.hhmm_to_minutes(config.QUIET_HOURS_START),
        routing.hhmm_to_minutes(config.QUIET_HOURS_END),
    )
    decision = routing.route_event(
        interruption_level=body.interruption_level,
        in_quiet_hours=in_quiet,
        nap_aware=config.NAP_AWARE,
        child_asleep=body.child_asleep,
    )
    if not decision.deliver:
        db.log_delivery(body.event, 0, decision.reason)
        return {"event": body.event, "delivered": 0, "suppressed": True,
                "reason": decision.reason}

    if decision.silent:
        payload = apns.build_background_payload(data={"event": body.event, **body.data})
        push_type = "background"
    elif body.interruption_level == "critical":
        payload = apns.build_critical_payload(
            title=body.title, body=body.body, category=body.category,
            data={"event": body.event, **body.data},
        )
        push_type = "alert"
    else:
        payload = apns.build_alert_payload(
            title=body.title, body=body.body, category=body.category,
            interruption_level=body.interruption_level,
            data={"event": body.event, **body.data},
        )
        push_type = "alert"

    results = []
    for dev in db.push_devices(body.household):
        if body.exclude_parent_id and dev["parent_id"] == body.exclude_parent_id:
            continue
        results.append(await _send_and_log(
            client, body.event, dev["device_token"], dev["env"], payload,
            push_type=push_type, collapse_id=body.collapse_id,
        ))
    delivered = sum(1 for r in results if r["ok"])
    pruned = sum(1 for r in results if r["pruned"])
    return {"event": body.event, "delivered": delivered, "suppressed": False,
            "silent": decision.silent, "pruned": pruned, "results": results}


@app.post("/v1/activity/start")
async def activity_start(body: ActivityStartBody, household: str = Depends(_household)):
    """Push-to-start a Live Activity on the OTHER parent's phone (iOS 17.2+)."""
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")
    payload = apns.build_liveactivity_payload(
        event="start", content_state=body.content_state,
        stale_date=body.stale_date, attributes_type=body.attributes_type,
        attributes=body.attributes,
    )
    results = []
    for dev in db.push_devices(body.household):
        if body.exclude_parent_id and dev["parent_id"] == body.exclude_parent_id:
            continue
        if not dev["push_to_start_token"]:
            continue
        results.append(await _send_and_log(
            client, "activity.start", dev["push_to_start_token"], dev["env"], payload,
            push_type="liveactivity",
        ))
    delivered = sum(1 for r in results if r["ok"])
    return {"event": "activity.start", "delivered": delivered, "results": results}


@app.post("/v1/activity/update")
async def activity_update(body: ActivityUpdateBody, household: str = Depends(_household)):
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")
    acts = db.activities_for(activity_id=body.activity_id, child_id=body.child_id)
    if not acts:
        raise HTTPException(status_code=404, detail="no matching activity")
    payload = apns.build_liveactivity_payload(
        event="update", content_state=body.content_state, stale_date=body.stale_date,
    )
    results = []
    for act in acts:
        results.append(await _send_and_log(
            client, "activity.update", act["push_token"], act["env"], payload,
            push_type="liveactivity",
        ))
    delivered = sum(1 for r in results if r["ok"])
    return {"event": "activity.update", "delivered": delivered, "results": results}


@app.post("/v1/activity/end")
async def activity_end(body: ActivityEndBody, household: str = Depends(_household)):
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")
    acts = db.activities_for(activity_id=body.activity_id, child_id=body.child_id)
    if not acts:
        raise HTTPException(status_code=404, detail="no matching activity")
    payload = apns.build_liveactivity_payload(
        event="end", content_state=body.content_state, dismissal_date=body.dismissal_date,
    )
    results = []
    for act in acts:
        results.append(await _send_and_log(
            client, "activity.end", act["push_token"], act["env"], payload,
            push_type="liveactivity",
        ))
        db.delete_activity(act["activity_id"])
    delivered = sum(1 for r in results if r["ok"])
    return {"event": "activity.end", "delivered": delivered, "results": results}


@app.post("/v1/test")
async def test_push(body: TestBody, household: str = Depends(_household)):
    """GUI 'send test notification' — also the 'Test critical alert' button (§7.7)."""
    client = apns.get_client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="APNs not configured")
    if body.critical:
        payload = apns.build_critical_payload(title=body.title, body=body.body)
    else:
        payload = apns.build_alert_payload(title=body.title, body=body.body)
    results = []
    for dev in db.push_devices(body.household):
        results.append(await _send_and_log(
            client, "test", dev["device_token"], dev["env"], payload, push_type="alert",
        ))
    delivered = sum(1 for r in results if r["ok"])
    return {"event": "test", "delivered": delivered, "results": results}


# ---- supervised watchdog (§7.7) ---------------------------------------------

@app.post("/v1/heartbeat")
async def heartbeat(body: HeartbeatBody, household: str = Depends(_household)):
    """The app checks in. Records last-heartbeat + the HA/Owlet health it observed so the
    watchdog can tell 'watching and fine' from 'quietly broken'."""
    now = time.time()
    db.set_monitoring("last_heartbeat", now)
    if body.ha_ok:
        db.set_monitoring("ha_last_seen", now)
    db.set_monitoring("owlet_unavailable", 1.0 if body.owlet_unavailable else 0.0)
    return {"status": "ok", "ts": now}


def _watchdog_decision(now: Optional[float] = None) -> routing.WatchdogDecision:
    now = now if now is not None else time.time()
    # last_push_error is set only on a real transport failure and cleared on the next
    # success, so it reflects the CURRENT health of the push channel (not a stale 410).
    undeliverable = bool(db.get_monitoring("last_push_error"))
    return routing.evaluate_watchdog(
        now=now,
        last_heartbeat=db.get_monitoring("last_heartbeat"),
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        ha_last_seen=db.get_monitoring("ha_last_seen"),
        ha_timeout=HA_TIMEOUT,
        owlet_unavailable=bool(db.get_monitoring("owlet_unavailable")),
        undeliverable=undeliverable,
    )


@app.post("/v1/watchdog/run")
async def watchdog_run(household: str = Depends(_household)):
    """Evaluate the monitoring chain; fire monitoring.chain_broken as a CRITICAL alert if
    any link is stale. Meant to be poked on a schedule (HA automation / cron)."""
    decision = _watchdog_decision()
    if not decision.fire:
        return {"fired": False, "status": decision.status, "reason": decision.reason}
    client = apns.get_client()
    fired_to = 0
    if client.is_configured():
        payload = apns.build_critical_payload(
            title="Monitoring stopped",
            body=f"Lulla can't confirm the baby is being watched: {decision.reason}.",
            data={"event": "monitoring.chain_broken", "reason": decision.reason},
        )
        for dev in db.push_devices():
            res = await _send_and_log(
                client, "monitoring.chain_broken", dev["device_token"], dev["env"],
                payload, push_type="alert", collapse_id="lulla-chain-broken",
            )
            if res["ok"]:
                fired_to += 1
    else:
        db.log_delivery("monitoring.chain_broken", 0, "APNs not configured")
    return {"fired": True, "status": decision.status, "reason": decision.reason,
            "delivered": fired_to}


@app.get("/v1/monitoring/status")
async def monitoring_status(household: str = Depends(_household)):
    """Status pip accessor for the app's Today screen (green/amber/red + last-checked)."""
    decision = _watchdog_decision()
    last_hb = db.get_monitoring("last_heartbeat")
    return {
        "status": decision.status,
        "reason": decision.reason,
        "healthy": not decision.fire,
        "last_heartbeat": last_hb,
        "checked_at": time.time(),
    }
