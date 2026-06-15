# ApexSight Push Bridge (Home Assistant addon)

Forwards your Frigate alerts to the [ApexSight push relay](../../push-relay) so
your iPhone gets **instant notifications even when the app is closed**. This
addon holds **no Apple secrets** — only your relay URL and a pairing code.

## Install (everything from the web UI)

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
   add `https://github.com/btoth525/apexsight-ha-addon`.
2. Install **ApexSight Push Bridge** from the store.
3. Open the **Configuration** tab — it's a normal form (no YAML to edit). Most of
   it is pre-filled; you only really set two things:

   | Field | What to enter | Default |
   |-------|---------------|---------|
   | **Relay URL** | Already correct — leave it | `https://relay.plexserver525.com` |
   | **Pairing code** | The `APEX-XXXX-XXXX` from ApexSight → Settings → Instant Push | — |
   | **Frigate URL (for the snapshot)** | A URL your phone can reach Frigate at, e.g. `https://frigate.yourdomain.com` | blank |
   | **Alerts only** | On = alerts only; Off = also detections | On |

   MQTT is filled in automatically from your Home Assistant broker — leave the
   `MQTT …` fields blank unless you run a separate broker.
4. **Start** the addon. Trigger some motion — your phone should buzz.

> The Configuration form validates as you type (e.g. the pairing code must look
> like `APEX-XXXX-XXXX`), so you can't start it half-configured.

## How it works

- Subscribes to MQTT `frigate/reviews` (configurable via `topic`).
- On a new alert review, builds the notification (title/body from the camera,
  objects, sub-labels and zones) and the image URL
  (`<frigate_base_url>/api/events/<detection>/preview.gif`).
- POSTs it to `<relay_url>/v1/notify` with your pairing code.
- The relay signs it with your `.p8` and delivers it via Apple APNs.

## Notes

- Each review notifies **once** (it dedupes across the new→update→end events).
- `frigate_base_url` must be reachable **from your phone's network** for the
  image to load. If your Frigate has authentication enabled, the ApexSight app
  supplies the token from its own session, so images still load.
- Requires the relay to be set up first, with your `.p8` uploaded.
