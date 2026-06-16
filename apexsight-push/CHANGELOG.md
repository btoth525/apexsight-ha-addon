# Changelog

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
