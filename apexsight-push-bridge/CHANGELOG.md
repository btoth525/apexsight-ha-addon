# Changelog

## 1.1.0

- `relay_url` and `pairing_code` now pre-filled with the shared defaults
  (`https://relay.plexserver525.com`, `APEX-PLEX-5250`) — for a shared-camera
  setup you only set the Frigate URL.
- Friendly GUI form via `translations/en.yaml`; pairing code validated.
- Added store branding (`icon.png`, `logo.png`).

## 1.0.0

- Initial release.
- Subscribes to Frigate's MQTT `frigate/reviews` and forwards new alerts to the
  ApexSight push relay (`/v1/notify`) with the household pairing code.
- Auto-discovers the Home Assistant MQTT broker (`services: mqtt:need`); manual
  `mqtt_*` overrides supported.
- Dedupes each review so it notifies once; builds the notification title/body
  from camera, objects, sub-labels and zones, plus the first detection's
  `preview.gif` / `snapshot.jpg` for the image.
