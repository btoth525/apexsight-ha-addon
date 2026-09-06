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
_admin_limiter = security.RateLimiter(max_attempts=5, window_seconds=300)


def _client_key(request: Request) -> str:
    # Honor Cloudflare's real-client-IP header when present (the tunnel proxies from it),
    # else fall back to the socket peer.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


@app.on_event("startup")
async def _startup() -> None:
    db.init()
    # Watch the Owlet sock and auto-log sleep sessions (single-writer → both phones get one
    # shared entry). Only when we actually have HA access; wrapped so it can never crash the app.
    if home.SUPERVISOR_TOKEN:
        asyncio.create_task(_owlet_sleep_poller())
        asyncio.create_task(_backfill_sleep_segments())


async def _backfill_sleep_segments() -> None:
    """Seed the hypnogram ONCE from HA's recorder so it doesn't launch empty.

    The recorder holds ~10 days; from here on the poller writes bands as they close and we keep
    them indefinitely (Owlet keeps session history forever — a chart that goes blank a fortnight
    back would be a step DOWN from what Taylor has today). Replayed through the same debounce the
    poller uses, so backfilled days are shaped identically to live ones — a seam there would show
    up as the chart changing character ten days back."""
    if db.get_config("owlet_backfill_done"):
        return
    try:
        readings = await home.sleep_state_history(days=10)
        if not readings:
            return          # sock never worn / recorder empty — try again next boot
        for seg in sleep_history.segments_from_readings(readings, until=time.time()):
            db.add_sleep_segment(seg.band, seg.start, seg.end)
        db.set_config("owlet_backfill_done", owlet_log.now_iso())
    except Exception:
        pass                # never let a backfill take the relay down


# Poll fast enough that "she just woke up" reaches a parent in seconds, not a minute. The HA
# Core API call is local and cheap, so 15s is comfortable for a household relay.
_OWLET_POLL_SECONDS = 15


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
    household = config.PAIRING_CODE
    while True:
        try:
            st = await home.state()
            vitals = st.get("vitals") or {}
            alerts = st.get("alerts") or {}
            baby = st.get("baby_name") or "Ryleigh"

            # 1) Relay Owlet's OWN alert flags — push once per OFF→ON episode. On the FIRST poll
            #    ever (no stored baseline) we seed silently, so a flag that's already on at deploy
            #    time (e.g. sock_off while it's charging) doesn't fire a spurious alert.
            raw_prev = db.get_config("owlet_alerts")
            if raw_prev is not None:
                for key in owlet_log.alert_transitions(json.loads(raw_prev), alerts):
                    await _push_owlet_alert(key, baby)
            db.set_config("owlet_alerts", json.dumps(alerts))

            now_ts = time.time()

            # 0) RESTART / GAP GUARD. The relay persists its debounce candidates and the open
            #    hypnogram band across a restart. Without this, a deploy or reboot mid-sleep would
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

            # 3a) AWAKE <-> ASLEEP, on the CONFIRMED edge only. Time-sensitive on wake (that's
            #     the one worth piercing Focus for, now that it's trustworthy); falling asleep
            #     stays a quiet note.
            if new_cls in ("awake", "asleep"):
                gap = (owlet_log.WAKE_ALERT_MIN_GAP if new_cls == "awake"
                       else owlet_log.ASLEEP_ALERT_MIN_GAP)
                key = f"owlet_{new_cls}_alert_ts"
                if now_ts - float(db.get_config(key) or 0) >= gap:
                    await _push_wake_state(new_cls == "awake", baby)
                    db.set_config(key, str(now_ts))

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
            if new_stage or new_cls:
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
                db.set_config("owlet_activity_start", owlet_log.iso_at(
                    now_ts - owlet_log.WAKE_HOLD_SECONDS))
                db.set_config("owlet_activity_stage_since", owlet_log.now_iso())
                await _sleep_activity_start(baby=baby, state=_content(stage_state.confirmed))
            elif new_cls in ("awake", "nosignal"):
                # End on wake OR sock-off. The old code ended only on "awake", so removing the
                # sock (nosignal) left an orphaned card counting up forever while the sleep log
                # had already closed — and the next sleep stacked a second card on top. Clearing
                # the live stage here keeps the next session from inheriting a stale headline.
                await _sleep_activity_push("end", _content(None))
                db.set_config("owlet_live_stage", "")
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

            # 3c-iii) Record the hypnogram band. Written off the SAME confirmed signals as the
            #         alerts and the auto-log, so the chart can never contradict them — an app
            #         that re-derived bands from the raw state would strobe and count ~70
            #         wakings for a night the log correctly calls eight.
            band = sleep_history.band_for(cur, stage_state.confirmed)
            # Back-stamp a band boundary caused by a CONFIRMED edge to when the change actually
            # started (now - hold), exactly as the auto sleep-log does — otherwise the chart would
            # show her asleep up to a full WAKE_HOLD (5 min) longer than the log at every waking,
            # and the two would visibly disagree on bedtime/wake. A class edge uses WAKE_HOLD; a
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
                # Keep the OPEN band's end fresh so a chart drawn mid-nap reaches "now" instead
                # of stopping at the last transition.
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
            pass   # a bad poll must never take the relay down
        await asyncio.sleep(_OWLET_POLL_SECONDS)


async def _push_wake_state(awake: bool, baby: str) -> None:
    """She just woke up / just fell asleep. Waking is time-sensitive (that's the one you want to
    catch through Focus — a feed usually follows); falling asleep is a quiet note."""
    try:
        await push(PushEventBody(
            event="owlet.awake" if awake else "owlet.asleep",
            household=config.PAIRING_CODE,
            title=f"{'👀' if awake else '😴'} {baby}",
            body="She's waking up." if awake else "She's fallen asleep.",
            interruption_level="time-sensitive" if awake else "passive",
            collapse_id="owlet-wake",
        ))
    except Exception:
        pass


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
    for dev in db.push_devices(config.PAIRING_CODE):
        if not dev["push_to_start_token"]:
            continue
        try:
            await _send_and_log(client, "activity.start", dev["push_to_start_token"],
                                dev["env"], payload, push_type="liveactivity")
        except Exception:
            pass


async def _sleep_activity_push(event: str, state: dict) -> None:
    """Update (or end) every running sleep activity. On `end` the registry row goes too, so a
    stale token can't keep a dead activity alive on the Lock Screen."""
    client = apns.get_client()
    if not client.is_configured():
        return
    acts = db.activities_by_kind(OWLET_ACTIVITY_KIND)
    if not acts:
        return
    payload = apns.build_liveactivity_payload(event=event, content_state=state)
    for act in acts:
        try:
            await _send_and_log(client, f"activity.{event}", act["push_token"], act["env"],
                                payload, push_type="liveactivity")
        except Exception:
            pass
        if event == "end":
            db.delete_activity(act["activity_id"])


async def _push_owlet_refresh(vitals: dict, *, stage: Optional[str],
                              sleep_class: str) -> None:
    """A silent (content-available) nudge carrying the current reading, so both phones can stamp
    their App Group snapshot and redraw the widget / Lock Screen immediately.

    Sent only on a CONFIRMED change (a handful of times a day), because iOS budgets background
    pushes and a per-poll nudge would simply be dropped. No alert, no sound, no badge — this is
    the data path behind the glance, not a notification."""
    client = apns.get_client()
    if not client.is_configured():
        return
    payload = apns.build_background_payload(data={
        "event": "owlet.refresh",
        "owlet": {
            "bpm": vitals.get("bpm"), "spo2": vitals.get("spo2"),
            "battery_pct": vitals.get("battery_pct"), "sock_on": vitals.get("sock_on"),
            "sleep_state": stage, "sleep_class": sleep_class,
            "read_at": owlet_log.now_iso(),
        },
    })
    for dev in db.push_devices(config.PAIRING_CODE):
        try:
            await _send_and_log(client, "owlet.refresh", dev["device_token"], dev["env"],
                                payload, push_type="background", collapse_id="owlet-refresh")
        except Exception:
            pass   # a silent nudge is best-effort by definition


async def _push_deep_sleep_reached(baby: str) -> None:
    """She just reached deep sleep and a parent armed the one-shot alert — "safe to put her
    down". Time-sensitive so it pierces Sleep Focus; this is the rare case where waking the phone
    is exactly what was asked for."""
    try:
        await push(PushEventBody(
            event="owlet.deep_reached", household=config.PAIRING_CODE,
            title=f"\U0001F634 {baby} is in deep sleep",
            body="Good window to put her down.",
            interruption_level="time-sensitive", collapse_id="owlet-deep-reached",
        ))
    except Exception:
        pass


async def _push_sleep_stage(state: str, baby: str) -> None:
    """A quiet, collapsing note that the baby moved to a new sleep stage. Passive so it never
    buzzes overnight; it just appears for a glance."""
    try:
        await push(PushEventBody(
            event="owlet.sleep_stage", household=config.PAIRING_CODE,
            title=f"😴 {baby}", body=f"Now: {owlet_log.stage_label(state)}",
            interruption_level="passive", collapse_id="owlet-stage",
        ))
    except Exception:
        pass


async def _push_owlet_alert(key: str, baby: str) -> None:
    """Fan an Owlet alert flag out to both phones (relaying the sock's own flag, not a threshold
    we invented). Safety-critical flags are time-sensitive so they pierce Sleep Focus."""
    meta = owlet_log.ALERT_META.get(key)
    if not meta:
        return
    phrase, critical = meta
    try:
        await push(PushEventBody(
            event=f"owlet.{key}", household=config.PAIRING_CODE,
            title=f"⚠️ {baby}", body=f"Owlet alert — {phrase}. Check the base station.",
            interruption_level="time-sensitive" if critical else "active",
            collapse_id=f"owlet-{key}",
        ))
    except Exception:
        pass   # APNs not configured / transient — never crash the poller


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
    push_env: Optional[str] = None            # "prod" | "sandbox"
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
    st = db.global_stats()
    return {
        "status": "ok",
        "service": "lulla-push",
        "pairing_code_set": bool(config.PAIRING_CODE),
        "apns_configured": apns.get_client().is_configured(),
        "records": st["records"],
        "devices": st["devices"],
        "push_devices": len(db.push_devices()),
        "households": st["households"],
    }


@app.post("/v1/register")
async def register(body: RegisterBody, request: Request):
    code = body.pairing_code.upper().strip()
    if config.ACCEPT_ANY_PAIRING:
        household = code                       # TEST mode: pairing code IS the household
    else:
        if not _register_limiter.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="too many attempts, try again later")
        if not security.safe_equals(code, config.PAIRING_CODE):
            raise HTTPException(status_code=403, detail="pairing code mismatch")
        household = config.PAIRING_CODE
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
    feed_day_hours: float
    feed_night_hours: float
    night_start: int
    night_end: int
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


@app.post("/v1/sync/push")
async def sync_push(body: PushBody, household: str = Depends(_household)):
    applied = 0
    max_seq = 0
    for r in body.records:
        res = db.upsert(household, r.type, r.id, r.updated_at, r.created_by,
                        r.is_tombstoned, r.payload)
        if res["applied"]:
            applied += 1
        max_seq = max(max_seq, res["server_seq"])
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
    of whether any matching entities exist yet."""
    return await home.state()


@app.get("/v1/home/sleep/sessions")
async def home_sleep_sessions(days: int = 7, household: str = Depends(_household)):
    """Sleep sessions + their hypnogram bands, newest first — everything the app needs to draw
    the chart and the session card without re-deriving anything.

    A "session" is what a parent means by one sleep: brief wakings stay INSIDE it and are
    counted, the way Owlet defines a waking, rather than being split into separate naps.
    """
    days = max(1, min(int(days), 120))
    since = time.time() - days * 86400
    segments = [sleep_history.Segment(r["band"], r["start_ts"], r["end_ts"])
                for r in db.sleep_segments(since)]
    sessions = sleep_history.sessions_from_segments(segments)
    sessions.sort(key=lambda s: s.start, reverse=True)
    return {
        "days": days,
        "backfilled": bool(db.get_config("owlet_backfill_done")),
        "segment_count": len(segments),
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


@app.post("/v1/register/activity")
async def register_activity(body: ActivityRegisterBody):
    db.register_activity(body.activity_id, body.child_id, body.kind, body.push_token, body.env)
    return {"status": "ok", "activity_id": body.activity_id}


@app.post("/v1/push")
async def push(body: PushEventBody):
    """Fan an event out to push devices, applying §7.4 routing (non-negotiable):
    never notify exclude_parent_id, collapse by collapse_id, respect quiet hours except
    time-sensitive, and downgrade non-urgent to silent when nap_aware + child asleep."""
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
async def activity_start(body: ActivityStartBody):
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
async def activity_update(body: ActivityUpdateBody):
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
async def activity_end(body: ActivityEndBody):
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
async def test_push(body: TestBody):
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
async def heartbeat(body: HeartbeatBody):
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
async def watchdog_run():
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
async def monitoring_status():
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
