# Changelog

## 1.15.0

**Hardening pass — admin auth bypass, a disk-fill DoS, and three delivery races.**

- **Admin login lockout was bypassable.** `admin.py`'s brute-force lockout trusted a
  client-supplied IP header unconditionally — a caller could forge a fresh fake IP on every
  attempt so the fail2ban-style lockout never triggered against any one key, enabling unlimited
  password guessing. A successful login exposes the plaintext household pairing code. Fixed by
  sharing the same trusted-proxy check `main.py`'s rate limiter already had (new `net_trust.py`).
- **Four endpoints let anyone write to the database with no pairing-code check**:
  `/v1/style`, `/v1/ai-cameras`, `/v1/recap`, `/v1/device-prefs`. Combined with `/v1/style`'s
  unbounded body, this was a disk-fill DoS (~1.9GB/min possible from one IP); `/v1/device-prefs`
  let a leaked device token alone silence that phone's real alerts. All four now require the
  household pairing code, matching every sibling mutating endpoint.
- **Rate-limiter crash under load**: the `_hits` eviction sweep could raise "dictionary changed
  size during iteration" under internet-scan-style traffic (an unhandled 500).
- **Duplicate alert / lost GIF-upgrade race**: a rapid run of Frigate review updates could
  dispatch the same alert twice, or silently lose the follow-up complete-GIF push forever if the
  review ended before the first alert confirmed delivery.
- **Daily recap could silently skip a day** on a same-day timezone change (travel) that crossed
  midnight before the scheduled hour — now catches up with a best-effort recap for the missed day.
- **Dead-token pruning could race a device re-registering** at the same moment, deleting its
  fresh registration instead of the actually-dead one.

## 1.14.1

- **Foreground mute re-check can now see the real object/zone.** `build_payload` forwards
  `label`/`zones`/`score` in the APNs `userInfo` (mirroring the same fields the per-device gate
  already evaluated server-side). The app's foreground `willPresent` re-check previously hardcoded
  a placeholder label since the push carried nothing to check against — a phone that muted a
  specific object type or zone (not the whole camera) still saw a foreground banner for it.

## 1.14.0

**Second hardening pass (audit findings).**

- **A transient APNs blip no longer silently drops a household's alert.** `/v1/notify` always returned
  HTTP 200 even when every phone's push failed for a transient reason (APNs timeout / 5xx / 429), so
  the bridge stamped the review "sent" and — the review having ended — never retried. It now returns
  503 when every reachable device failed transiently (not a dead-token prune, not a per-device mute),
  so the bridge's existing retry fires. This was the worst failure mode for a security app.
- **Daily-recap DoS closed.** `/v1/recap` clamps `tz_offset` to ±24h and `hour`/`minute` to valid
  ranges — an out-of-range value made `datetime.timezone` raise every scheduler tick and killed the
  household's recap until manually overwritten.
- **SQLite WAL + busy timeout**, so a burst of alerts × several phones while the bridge writes recap
  events can't surface as "database is locked" 500s.
- **play-url SSRF hardening**: the ffmpeg network input now carries a protocol whitelist so a
  malicious redirect can't pivot to `file://` (local-file exfil) or exotic schemes.
- **TURN error no longer leaks the Cloudflare Key ID** to the (paired) caller — logged server-side only.

Known/accepted (not changed): `/v1/gate` can suppress camera-detection pushes with just the pairing
code — that's the intended "pause my notifications" household toggle (a real break-in still alarms via
the separate HA critical channel), so it is deliberately NOT gated behind the Alarmo code.

## 1.13.1

- **Test push no longer dead-ends or bumps the badge.** The in-app "Test" button's push carried a
  synthetic `review_id` and an `apex://review?id=relay-test` link — so tapping it opened a review
  detail that 404s, and the app-icon badge ticked up for a diagnostic. It now drops the review id
  (no badge bump) and opens the camera wall (`apex://cameras`) on tap.

## 1.13.0

**Security + delivery-reliability audit fixes.**

- **Auth gates (were missing):** `/v1/mode` (house mode — drives the camera mute filter),
  `/v1/set-mode` arming, and `/v1/doorbell-ring` now all require the household pairing code —
  previously an unauthenticated request could silence every camera alert, arm the alarm, or ring
  all phones. Legitimate callers (the bridge, the app) already send the code, so nothing breaks.
  The `/v1/doorbell-ring` fallback to the configured code was removed.
- **Rate limiter now keys on the real client IP** (`cf-connecting-ip`/`x-forwarded-for`) instead of
  the shared tunnel address, and never throttles the in-house bridge — so an attacker can no longer
  exhaust one global bucket and cause the relay to 429 real alerts.
- **Alert delivery is now confirmed before dedup:** the bridge stamps a review stage as "sent" only
  after the relay returns 2xx, and retries 429s — previously a relay restart or a rate-limit could
  permanently drop an alert (its later updates saw it as already sent). This is the worst failure
  mode for a security app; it's closed.
- **SSRF guard on `/v1/doorbell/play-url`:** external (tunnelled) callers can no longer point the
  relay's ffmpeg at private/loopback hosts. The in-house bridge is exempt so LAN-hosted Home
  Assistant TTS still plays at the door.
- **Snooze is bounded** (≤24 h) so a leaked pairing code can't silence notifications indefinitely.
- **Badge accuracy:** replace/announce/final pushes are flagged `no_badge` so the app-icon badge
  stops over-counting AI-description follow-ups.
- **Daily recap** is only marked sent when a phone actually received it (retries instead of skipping
  the day on a transient APNs blip).

## 1.12.0

**LIVE hold-to-talk to the doorbell speaker — real two-way conversation.**

- **New `POST /v1/doorbell/talk-live`**: pipes the app's live microphone straight to the Aqara
  doorbell speaker. The app publishes its mic into go2rtc's `apex_talkback` stream over WebRTC,
  then calls this; the relay pulls that stream (RTSP from the Frigate host, derived from
  `frigate_base_url`) and streams it to the doorbell over the existing native Aqara LAN talkback
  protocol. Releasing the talk button ends the publish → the stream EOFs → the session closes
  cleanly through the normal tail/drain path.
- Guards: household pairing code required (same as all talkback), waits up to ~3s for the mic
  publish to actually land in go2rtc before opening the door speaker (no phantom speaker pops),
  single-session lock shared with clip playback (busy → 409), 120s hard backstop per hold.
- No `-re` pacing on the live input (it's already real-time), unlike clip playback.
- Server prerequisite: an `apex_talkback:` stream entry (no sources) in Frigate's go2rtc config —
  the app's WebRTC publish is the producer (already applied to the household Frigate).

## 1.11.0

**Remote full-res WebRTC via Cloudflare TURN — live view works away from home on Frigate 0.18.**

- Frigate 0.18 removed the go2rtc HLS live route, so live view is now WebRTC-only. WebRTC media
  can't cross the household's Cloudflare HTTP tunnel — on the LAN it reaches go2rtc's host
  candidate directly, but away from home that candidate is unreachable, so every remote live tile
  went black. The app was already built to fetch TURN relay credentials from the relay; that
  endpoint just didn't exist yet.
- **New `POST /v1/turn-credentials`** mints short-lived **Cloudflare Realtime TURN** ICE servers
  (gated by the household pairing code) so the phone can relay full-res, sub-second WebRTC through
  Cloudflare's edge when direct fails. The relayed media is end-to-end DTLS-encrypted — Cloudflare
  forwards packets but cannot see the camera. Creds are cached and shared household-wide, re-minted
  only near expiry, so a wall of phones opening streams doesn't hammer the Cloudflare API.
- **Setup:** create a TURN key in Cloudflare → Realtime → TURN, then set `turn_key_id` and
  `turn_api_token` in the add-on Configuration. Free tier is 1,000 GB/month (TURN is only used
  away from home; on the LAN the connection is direct and free).


## 1.10.8

**Widgets follow the house mode with the app closed.**

- When the house mode actually changes, the relay now fans a **silent background push** to every
  phone — the app wakes for a second, refreshes the shared mode, and repaints the Lock Screen
  widget + Control Center. Before this, those surfaces only updated when the app was opened
  (they're fed by the app's foreground poll), so arming from HA/the keypad left them stale.
- No banner, no sound — it's a content-available push (APNs `background`/priority-5), a few per
  day at most.


## 1.10.7

Hardening pass from a full audit of 1.10.5/1.10.6 (all confirmed-by-review fixes):

- **Fix: a failed ring forward no longer eats the visitor's retry press.** The debounce window is
  now only charged on a CONFIRMED 2xx from the relay — previously a relay blip meant the first
  press rang nobody AND the retry press was debounced, so the doorbell was silent for the whole
  window.
- **Fix: a failed Frigate switch sync now retries within seconds instead of being recorded as
  done.** HA errors (restart, 401/500) were swallowed and the sync marked complete — Frigate's
  per-camera alert switches could sit wrong (including cameras left MUTED in Away) for up to 10
  minutes. The watcher now verifies both service calls succeeded and retries every 3s until they do.
- **Fix: the mode editor's camera roster fallback now includes the never-muted cameras**
  (Front Driveway, doorbell) — a union of mute lists alone omitted exactly the always-alerting ones.


## 1.10.6

**Doorbell ring debounce — button mashing rings once, not over and over.**

- A visitor re-pressing the doorbell while your phones are already ringing used to restart the
  CallKit call each press. Re-presses are now dropped at the bridge (no VoIP push is even sent)
  for `doorbell_ring_debounce` seconds after the last forwarded ring — the industry-standard
  behavior (Ring/Nest ring once per visit). New option, default **30s**, 0 disables.

## 1.10.5

**Editable per-mode camera alerts (household-wide) + snooze visibility.**

- **New: the house-mode camera alert matrix is now editable from the app** (Settings →
  Notifications → House Mode Alerts) instead of hardcoded. `POST /v1/mode-map` stores the
  household's per-mode mute lists (one map per pairing code — every phone follows it);
  `GET /v1/mode` now returns the full effective matrix (`map`, `map_custom`, `cameras`) so the app
  can show exactly what alerts in each mode. Fail-open preserved: mute-lists, so a new camera
  always alerts until explicitly turned off. Reset-to-defaults supported.
- **New: the bridge mirrors the matrix into Frigate itself.** On every house-mode or map change
  (and a 10-min self-heal), the bridge flips each camera's `review_alerts`/`review_detections`
  switches via the HA API so Frigate stops creating alerts for muted cameras — the app, relay,
  HA and the Frigate PWA all agree. This replaces the hardcoded lists in the
  "Frigate Alerts Follow House Mode" HA automation (which can be disabled once this runs).
- **Fix: household snooze/disarm is now VISIBLE.** `GET /v1/mode?pairing_code=…` reports
  `snoozed_until`/`disarmed`, and the app shows a loud banner ("All notifications snoozed …"),
  with one tap to resume — a snooze set from Siri/a widget/a partner's phone used to silently
  eat every alert with no indication anywhere ("why am I not getting notifications").
- **Fix: dead tokens registered under the wrong APNs environment are pruned** (the
  `403 BadEnvironmentKeyInToken` device that failed on every single push).

## 1.10.4

Add: playing a saved doorbell clip now also flips the matching **Doorpanel** screen (screen + voice
from one tap in the app's soundboard). Maps clip slug → panel button via the Supervisor HA API
(no-op if unmapped or the panel isn't installed). Slugs → screens:
no-soliciting deterrents (recorded-warning/not-interested/you-were-warned/nice-try) → GO AWAY;
be-right-there / leave-it-at-the-door-please / thanks-delivery → their friendly screens.


## 1.10.3

Fix: doorbell talkback cutting off words "all over the place" (sometimes the first word, sometimes
the last, inconsistently). This is the real cause behind what 1.10.2's silence padding only partly
masked.

- Root cause: the RTP audio was sent as a **burst**, not a real-time stream. `ffmpeg -re` was
  supposed to pace the clip to real time but doesn't here — it dumps the entire clip into the pipe
  in ~30ms — so all ~26 audio packets hit the camera at once. The doorbell's jitter buffer is sized
  for a live ~16 kHz stream (one 64ms frame every 64ms); a flood overruns it and it drops whatever
  won't fit, which is why the lost words were random rather than always the first.
- Fix: the relay now **meters the whole RTP stream itself** on a monotonic schedule — packet N
  leaves at start + N×64ms — instead of trusting ffmpeg's pacing. Silence lead-in, the clip, and
  the silence tail are one continuous, evenly-paced stream, exactly what the camera expects.
- Also sets the RTP **marker bit** on the first packet (start-of-talkspurt), so the camera opens
  its speaker / resets playout cleanly at the top of each clip.

## 1.10.2

Fix: doorbell talkback / "Say" no longer cuts off the first and last words.

- The Aqara opens its speaker ~0.5–0.8s **after** the START_VOICE handshake and primes on the RTP
  audio stream itself, so streaming the clip immediately swallowed the first word; and firing
  STOP_VOICE the instant ffmpeg reached EOF flushed the decode buffer before it had played the
  last word. Short phrases (a TTS "hello" or a one-word name) lost roughly half of what was said.
- The full audio was always transmitted — verified the ffmpeg encode is faithful (a 2.0s source
  yields 2.1s / 33 frames) — so this was purely a camera playback-window problem, not truncation.
- Fix: bracket the real audio with **paced AAC silence** — a ~0.8s lead-in warms the speaker while
  wall-clock elapses, a ~0.5s tail keeps the RTP stream alive so the buffer drains, then a short
  pause before STOP_VOICE. RTP sequence + timestamps stay continuous across the padding, so the
  camera sequences playback correctly. (An ffmpeg `-af adelay` lead-in was tried first and rejected:
  under `-re` it emits non-monotonic DTS and the silence is dropped entirely.)
- Silence frames are generated once via ffmpeg and cached; if ffmpeg can't run the padding is
  simply skipped (same behavior as before), never a failure.
- The three windows are **tunable from the add-on Configuration tab** — `doorbell_lead_ms` (800),
  `doorbell_tail_ms` (500), `doorbell_drain_ms` (300) — since the exact warm-up is firmware-
  dependent. If a word is still clipped, raise the matching value and restart; no rebuild needed.

## 1.10.1

Hardening pass on 1.10.0's talkback (full audit):

- **Fix: the HA "Reachable" sensor no longer opens a voice session on the camera.** The bridge
  polls reachability every ~30s; the probe used the real START_VOICE handshake, which could
  collide with (or cut off) a clip actually playing. It's now a plain TCP connect — non-invasive.
- **Fix: playing a clip can no longer delay the doorbell RING.** Talkback commands from HA ran on
  the MQTT loop thread, so a 30s clip blocked ALL message handling — including the ring →
  CallKit push — until it finished. They now run on worker threads. (Same for the entity publish
  on reconnect.)
- **Fix: a hanging play-url source can no longer wedge talkback until restart.** ffmpeg gets a
  10s network I/O timeout + a hard output-duration cap, plus a kill-timer backstop, so the play
  loop always terminates and the camera's one voice session is always released.
- **One clip at a time, cleanly.** Concurrent plays (app clip + HA say at once) used to collide on
  the camera; the second play now fails fast with 409 "talkback busy".
- Camera-busy/no-answer handshake failures now return a proper 502 instead of a raw 500; upload
  size is enforced without buffering an oversized body; preset saves are atomic + lock-protected
  (a crash can no longer orphan every saved preset); re-saving a preset in a different format no
  longer strands the old file; talkback endpoints refuse to run if the relay has no pairing code.

## 1.10.0

- **Talk to the doorbell — play audio to the Aqara G400 speaker.** The relay can now speak the
  Aqara camera's LAN talkback protocol directly (TCP :54324 control + UDP :54323 AAC-LC RTP, no
  cloud/hub/auth), so the app can send audio to the doorbell. Foundation for two-way talk + a
  soundboard of pre-recorded/recorded clips.
  - New endpoints: `POST /v1/doorbell/clip` (upload an audio clip → plays now, optionally saves it
    as a preset), `GET /v1/doorbell/clips` (list saved presets), `POST /v1/doorbell/play` (play a
    saved preset), `POST /v1/doorbell/delete`, and `GET /v1/doorbell/status`
    (configured + reachable). All require the household pairing code; talkback actuates the door
    speaker so the code is enforced.
  - Any format the app sends is transcoded with ffmpeg to the camera's required AAC-LC ADTS,
    16 kHz mono. Saved presets live in the add-on's `/data` volume.
  - **New options:** `doorbell_ip` (the camera's LAN IP — set this to enable talkback) and
    `doorbell_gain` (playback loudness multiplier, default `3.0`). ffmpeg is now bundled.
  - Protocol validated live against a real G400.
- **Exposed to Home Assistant.** The bridge publishes an "ApexSight Doorbell" device via MQTT
  discovery so you can talk to the door from HA too: a **Reachable** connectivity sensor, a **Last
  Talkback** sensor, a **Say** text box (type text → the door speaks it), and a **press-button per
  saved clip** (press → it speaks at the door). Three command topics let automations speak:
  `apexsight/doorbell/say` (plain text → local TTS → door), `apexsight/doorbell/play_url` (any
  audio/TTS media URL), and `apexsight/doorbell/play_clip` (a preset slug). Entities self-heal
  (retained, republished each cycle).
- **Local text-to-speech.** With `homeassistant_api: true`, the add-on renders `say` text through
  Home Assistant's TTS (`tts_get_url`) and plays it at the door — pair it with the **Piper** add-on
  for a fully-local neural voice (no cloud). Engine is configurable via `doorbell_tts_engine`
  (default `tts.piper`; also accepts a legacy platform like `google_translate`).

## 1.9.0

- **Doorbell ring → a real CallKit call.** When the doorbell button is pressed, the relay now sends
  a PushKit **VoIP** push so the phones ring with the native full-screen incoming-call screen (works
  on the Lock Screen), and answering opens ApexSight's live doorbell view + two-way talk.
  - New `POST /v1/register-voip` (the app registers its VoIP token) and `POST /v1/doorbell-ring`
    (fans a VoIP push to every phone in the household). The bridge forwards MQTT `apexsight/doorbell`
    (published by an HA automation on the ring sensor) to it.
  - VoIP pushes reuse the same `.p8` key but the `<bundle>.voip` topic + `voip` push type; dead
    VoIP tokens are pruned like APNs tokens.

## 1.8.2

- **Fix: House Mode / Armed By sensors could fail to appear.** They published their MQTT discovery
  config only once — the same connect-race that hid the phone entities in 1.7.0. They now
  re-publish every cycle (retained, idempotent) so they self-heal, exactly like the phone entities.
  Entity names trimmed to "Mode" / "Armed By" (the device already reads "ApexSight House").

## 1.8.1

- **Snappier arm/disarm.** The bridge now checks for a pending app arm/disarm request every 1s
  (was 2s), so the round trip the app waits on is a second shorter.

## 1.8.0

- **House Mode + Who-Armed sensors, and arm/disarm from the app.** The relay/bridge now expose the
  home's arm stage to HA and accept arm/disarm requests from the iOS app.
  - **`sensor.apexsight_house_mode`** — Home / Night / Away (the actual arm stage), with the muted
    cameras for that mode as an attribute. **`sensor.apexsight_armed_by`** — who last set it, and
    when. Both under a new "ApexSight House" device.
  - **Richer phone entities.** Each phone now also gets an **online** connectivity binary sensor,
    and its sensor carries live notification posture — `notifications` (Active / Snoozed /
    Disarmed), `muted_cameras` count, and `quiet_hours` — real state to automate on.
  - **Arm/disarm from the app.** New `POST /v1/set-mode`; the bridge forwards each request to HA
    over `apexsight/mode/set` for an automation to arm Alarmo. Requests are **consume-once** (a
    monotonic seq, remembered across restarts) so a bridge restart can never re-fire a stale
    disarm. **Security:** disarming (Home) requires the Alarmo code in the request — validated by
    Alarmo itself — so the public pairing code alone can never drop the alarm; arming rides the
    pairing code. The disarm code is scrubbed from storage right after it's forwarded.

## 1.7.1

- **Fix: per-phone entities never appeared.** The bridge published each phone's MQTT discovery
  config exactly once — but that first publish could fire in the split second before the MQTT
  socket finished connecting, so it was dropped and the entity never showed up. The bridge now
  publishes only while actually connected, re-publishes the retained discovery every cycle (and
  once immediately on each connect), so the entities appear promptly and self-heal across any
  reconnect or broker restart. Logs `published N phone entities to HA`.

## 1.7.0

- **Every phone shows up in Home Assistant, by name.** Each iPhone paired to the relay is now
  published to HA as its own entity via MQTT discovery — grouped under one "ApexSight Phones"
  device, named whatever you set the phone to in the app ("Brandon's iPhone", "Taylor's iPhone").
  The entity is a last-seen timestamp sensor with the phone's pairing, environment and online
  status as attributes. This is the foundation for the "who armed" + arm-from-app automations.
  - The relay stores each device's name (new `device_name` column; migrated in place); the app
    sends it on register and keeps it fresh on the foreground sync. A name-only refresh never
    touches the device's APNs environment.
  - The bridge (the process with the MQTT connection) reconciles the entities against the device
    list every ~30s, retained, and clears the entity for any phone that unregisters.

## 1.6.0

- **House mode → camera alerts.** The relay now applies the home's Alarmo mode (Home / Night /
  Away) as a house-level camera filter on app-closed pushes. Home alerts only Front Driveway +
  Doorbell; Night adds Side Gate, Backyard and Garage; Away alerts every camera. Frigate keeps
  recording + detecting everything in all modes — only alerting changes.
  - HA publishes the mode to MQTT `apexsight/mode` (retained) on each Alarmo state change; the
    bridge forwards it to the new `POST /v1/mode`; `/v1/notify` suppresses cameras that mode mutes.
  - Modes are stored as MUTE-lists (a camera you add later defaults to alerting), and the whole
    layer is FAIL-OPEN: an unknown/blank mode, or an unrecognized value, delivers everything. Away
    mutes nothing — the safe default. This gates only camera-detection pushes; the alarm-triggered
    critical alert is a separate HA channel and always fires.

## 1.5.0

- **Full per-device notification gate, app-closed.** The relay now evaluates every device's own
  notification preferences before delivering — per-camera, per-object, and per-zone mutes, quiet
  hours, per-camera snoozes, and custom triggers (which can re-open a muted combo, exactly like the
  app). Each device syncs its prefs via `POST /v1/device-prefs` (keyed by device token); the gate
  mirrors the app's `wouldDeliver` decision. Previously only Disarm, global Snooze, and a whole-camera
  mute were honored app-closed.
- **Fail-open by design.** Any uncertainty — no prefs synced yet, malformed data, unknown camera,
  timezone in doubt — delivers. A missed alert is never acceptable in a security/baby-monitor system;
  the only way to suppress is an affirmative, confident mute. Disarm and global Snooze-all stay
  real-time household state (they're settable from Siri/widgets), so a stale per-device blob can
  never suppress a live alert after a re-arm.
- Every delivery decision is logged per device (`[gate] <token>… deliver/SUPPRESS: <reason>`) so
  behavior is auditable in the add-on logs.

## 1.4.2

- **Correct snapshot/GIF on notifications.** The bridge picked `detections[0]` (raw MQTT
  order) for the notification image, which on a multi-detection review is frequently the
  wrong moment — Frigate re-links long-lived parked-car tracks into fresh reviews. It now
  selects the detection at the review's canonical thumbnail moment (`thumb_time`), matching
  the iOS app's own selection. Verified against live reviews (e.g. a 16-detection review went
  from 32s off to 1s off).
- **Per-camera notification mute now works with the app closed.** New `POST /v1/muted-cameras`
  lets the app sync which cameras have notifications turned OFF; the relay suppresses pushes for
  those cameras at delivery time. Previously the per-camera toggle only gated foreground
  delivery, so a disabled camera still buzzed when the app was closed.
- **Fixed a dropped-alert on escalation.** A review first seen as a plain `detection` had its
  alert stage marked "already sent" while `alerts_only` posted nothing — so when it later
  escalated to `alert`, the real alert was deduped and never delivered. The alert stage is now
  gated on `alert` severity (under `alerts_only`), matching the delivery filter.

## 1.3.0

- **Daily Recap with the app closed.** The relay now sends the once-a-day summary
  itself at your chosen local time, so it arrives even when the app has been closed
  for hours (background refresh can't be relied on for a fixed time). The app syncs
  the schedule via `POST /v1/recap`.
- **Built entirely from MQTT — no Frigate HTTP query, no extra config or auth.** The
  bridge now also subscribes to `frigate/events` and accumulates the day's activity
  into the shared DB; the relay summarizes it (events, who was seen, busiest camera,
  deliveries) and pushes the recap.
- The in-app local recap now stands down whenever instant push is configured, so the
  daily summary is never delivered twice.

## 1.2.0

- **Disarm / Snooze now silence app-closed pushes.** The relay honors a per-household
  gate the iOS app syncs via the new `POST /v1/gate` endpoint: when you Disarm or
  Snooze (from the app, a widget, Siri or CarPlay) the relay suppresses delivery
  instead of pushing anyway — matching the in-app behavior. (Changes made while the
  app is fully closed take effect on the next time you open it.)

## 1.1.0

- **App-controlled notification style.** The relay now renders title / body / media
  from raw event fields using a per-household style the iOS app saves via the new
  `POST /v1/style` endpoint — so the in-app Alert Style screen shapes even
  app-closed instant pushes. Defaults mirror the SgtBatten blueprint (full emoji
  map, `entities · zone · confidence% · time`, sub-label-first).
- **Cropped snapshot → full GIF.** Instant alert carries a tight cropped bbox
  snapshot; the review-end "final update" swaps in the complete animated GIF via
  the shared APNs collapse id (no duplicate notification).
- **Lock-screen actions** (View Live / Review / Silence) supported via the payload.
- **Reliability:** the bridge retries relay POSTs with backoff so a transient blip
  no longer drops an alert; quieter "final update" pushes (passive, no second buzz).

## 1.0.0

- Initial release. All-in-one add-on: APNs relay (FastAPI + web GUI) **and** the
  Frigate MQTT bridge in one container, for Home Assistant OS.
- OPEN WEB UI button → admin page (upload `.p8`, set Key/Team/Bundle IDs, devices,
  test push). Username + password login with brute-force lockout.
- Bridge auto-discovers the HA MQTT broker and forwards `frigate/reviews` alerts
  to the local relay; pairing code pre-filled with the shared default.
- Publishes port 3421 for the public API; APNs key + registrations stored in the
  add-on's persistent `/data` volume.
