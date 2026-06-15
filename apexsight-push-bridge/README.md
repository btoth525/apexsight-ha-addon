# ApexSight Push Bridge (Home Assistant addon)

Forwards your Frigate alerts to the [ApexSight push relay](../../push-relay) so
your iPhone gets **instant notifications even when the app is closed**. This
addon holds **no Apple secrets** — only your relay URL and a pairing code.

## Install (plug-and-play)

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
   add:
   ```
   https://github.com/btoth525/apexsight
   ```
2. Install **ApexSight Push Bridge** from the store.
3. Open the addon **Configuration** tab and set:

   | Option | What to enter |
   |--------|---------------|
   | `relay_url` | Your relay, e.g. `https://push.yourdomain.com` |
   | `pairing_code` | The code shown in ApexSight → Settings → Instant Push |
   | `frigate_base_url` | A URL where your phone can reach Frigate, e.g. `https://frigate.yourdomain.com` (used for the notification image) |
   | `alerts_only` | `true` = only alerts; `false` = also detections |

   MQTT is filled in automatically from your Home Assistant broker. Override
   `mqtt_host` / `mqtt_port` / `mqtt_user` / `mqtt_password` only if you run a
   separate broker.
4. **Start** the addon. Trigger some motion — your phone should buzz.

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
