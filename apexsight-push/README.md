# ApexSight Push (Home Assistant add-on)

**One add-on that does everything** for instant iOS notifications: it runs the
APNs **relay** (with a web GUI to upload your Apple `.p8`) **and** the Frigate
**bridge** (forwards alerts) in a single container. Perfect for Home Assistant OS
— no separate Docker host needed.

## Install

1. HA → **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/btoth525/apexsight-ha-addon`.
2. Install **ApexSight Push**.
3. **Configuration** tab:
   - **Admin username / password** — for the web GUI login.
   - **App API token** — leave blank to auto-generate (shown on the dashboard),
     or set your own. The app + bridge use it to authenticate to the relay.
   - **Frigate URL** — where your phone can reach Frigate (for the alert image).
   - **Pairing code** — your household code; it must match the app's code
     (Settings → Instant Push). There is no shared default.
   - MQTT auto-fills from your HA broker.
4. **Start**. First launch builds Python — watch the **Log** until you see
   `Uvicorn running on http://0.0.0.0:3421` and `connected to MQTT … subscribing`.
5. Click **OPEN WEB UI** → sign in → **Settings** → upload your `.p8`, paste your
   **Key ID** + **Team ID** → Save. Dashboard shows **APNs ● Configured**, and
   the **App API token** to copy into the ApexSight app.

## Make it reachable from the internet

The iOS app talks to the relay, so it needs a public HTTPS hostname. Point your
**Cloudflare Tunnel** (or any reverse proxy) at the add-on's host port:

- Service: `HTTP` → `http://<your-HA-ip>:3421`
- **Path: leave empty.**
- Public hostname: e.g. `relay.plexserver525.com`.

Then `https://relay.plexserver525.com/healthz` returns `{"ok": true}`, and the
app's Instant Push screen turns 🟢.

> **Don't** also port-forward `3421` on your router — reach the relay *only*
> through the tunnel. See [`../SECURITY.md`](../SECURITY.md) for why, plus how to
> put Cloudflare Access in front of the admin page.

### Changing the port

The relay listens on `3421` inside the container. To use a different host port,
change it in the add-on's **Network** section — the OPEN WEB UI button and your
tunnel target should use that same host port.

## Notes

- Your `.p8`, Key ID, Team ID and the API token live only in the add-on's
  persistent `/data` volume — never in this repo.
- The public `/v1` API requires the **App API token** (`Authorization: Bearer`);
  it fails closed without it. The bridge posts locally (`127.0.0.1:3421`) with
  the same token; the web GUI login is brute-force protected.
- Builds for `aarch64` (Pi 4/5, most HA OS) and `amd64` (NUC/x86).
