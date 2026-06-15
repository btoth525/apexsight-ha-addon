# Changelog

## 1.0.0

- Initial release. Runs the ApexSight push relay (FastAPI + web GUI) as a native
  Home Assistant add-on for HA OS.
- OPEN WEB UI button → admin page (upload `.p8`, set Key/Team/Bundle IDs, view
  devices, send a test push). Username + password login with brute-force lockout.
- Publishes port 3421 for the public API (`/v1/register`, `/v1/notify`, `/healthz`)
  so the app + bridge can reach it via your tunnel/proxy.
- APNs key + registrations stored in the add-on's persistent `/data` volume.
