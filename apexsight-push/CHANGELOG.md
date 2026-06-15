# Changelog

## 1.0.0

- Initial release. All-in-one add-on: APNs relay (FastAPI + web GUI) **and** the
  Frigate MQTT bridge in one container, for Home Assistant OS.
- OPEN WEB UI button → admin page (upload `.p8`, set Key/Team/Bundle IDs, devices,
  test push). Username + password login with brute-force lockout.
- Bridge auto-discovers the HA MQTT broker and forwards `frigate/reviews` alerts
  to the local relay; pairing code pre-filled with the shared default.
- Publishes port 3421 for the public API; APNs key + registrations stored in the
  add-on's persistent `/data` volume.
