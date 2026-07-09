# Changelog

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
