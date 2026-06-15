# ApexSight Push Relay (Home Assistant add-on)

Runs the ApexSight **push relay** natively on Home Assistant OS — no separate
Docker host needed. It holds your Apple APNs key, registers your devices, and
signs the pushes that the ApexSight Push Bridge forwards. Includes a web GUI
(the **OPEN WEB UI** button) to upload your `.p8` and manage devices.

## Install

1. HA → **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/btoth525/apexsight-ha-addon` (same repo as the bridge).
2. Install **ApexSight Push Relay**.
3. **Configuration** tab → set **Admin username** + **Admin password**. Start it.
4. Click **OPEN WEB UI** → sign in → **Settings** → upload your `.p8`, paste your
   **Key ID** + **Team ID** (Bundle ID is pre-filled). Save. The dashboard should
   show **APNs ● Configured**.

## Make it reachable from the internet

The app and the bridge talk to the relay over the network, so it needs a public
HTTPS hostname. Point your **Cloudflare Tunnel** (or any reverse proxy) at the
add-on's host port:

- Service: `HTTP` → `http://<your-HA-ip>:3421`  (e.g. `http://192.168.1.203:3421`)
- **Path: leave empty** (must match all paths, not just one).
- Public hostname: `relay.plexserver525.com`.

Then `https://relay.plexserver525.com/healthz` should return JSON, and the app's
Instant Push screen turns 🟢.

### Changing the port

The relay listens on `3421` inside the container. To publish a different host
port, change it in the add-on's **Network** section (HA maps it for you) — the
OPEN WEB UI button and your tunnel target should use that same host port.

## Notes

- Your `.p8`, Key ID and Team ID live only in the add-on's persistent `/data`
  volume (uploaded via the GUI) — never in this repo.
- The admin login is brute-force protected (5 bad tries from an IP → 15 min lockout).
- This add-on is the relay; the **ApexSight Push Bridge** add-on is the forwarder.
  Install both: the bridge sends Frigate alerts to this relay.
