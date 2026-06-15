# ApexSight — Home Assistant Add-on Repository

Two add-ons that give your iPhone **instant Frigate notifications even when the
ApexSight app is closed**. Published at
**https://github.com/btoth525/apexsight-ha-addon**.

- **ApexSight Push Relay** — runs the relay (holds your Apple `.p8`, signs the
  pushes) natively on HA OS, with an **OPEN WEB UI** button to upload your key.
  Install this if you don't have a separate Docker host.
- **ApexSight Push Bridge** — forwards Frigate's MQTT alerts to the relay. Holds
  no Apple secrets — just the relay URL + a pairing code.

Install **both**: the Relay first (upload your `.p8`), then the Bridge.

```
apexsight-ha-addon/            ← repo root (what HA reads)
├── repository.yaml
├── README.md
└── apexsight-push-bridge/
    ├── config.yaml
    ├── build.yaml
    ├── Dockerfile
    ├── run.sh
    ├── bridge.py
    ├── CHANGELOG.md
    └── README.md
```

## Install it (testers)

1. HA → **Settings → Add-ons → Add-on Store → ⋮ (top-right) → Repositories**.
2. Paste `https://github.com/btoth525/apexsight-ha-addon` and **Add**.
3. **ApexSight Push Bridge** appears in the store → **Install**.
4. Open it → **Configuration**:

   | Option | Value |
   |--------|-------|
   | `relay_url` | `https://relay.plexserver525.com` |
   | `pairing_code` | The code shown in ApexSight → Settings → Instant Push |
   | `frigate_base_url` | A URL your phone can reach Frigate at (for the snapshot/GIF) |
   | `alerts_only` | `true` = only alerts; `false` = also detections |

   MQTT is auto-filled from the HA broker; override `mqtt_*` only for a separate broker.
5. **Start** the add-on. Trigger motion → instant rich notification, app closed. 🎉

## Publish / update it (maintainer)

The canonical source lives in the main app repo under `homeassistant-addon/`.
To push it to the dedicated add-on repo:

```bash
git clone https://github.com/btoth525/apexsight-ha-addon
cd apexsight-ha-addon
# copy the contents of this folder (ApexSight repo → homeassistant-addon/) to the root:
cp -r /path/to/ApexSight/homeassistant-addon/. .
git add .
git commit -m "ApexSight Push Bridge 1.0.0"
git push
```

When you change the add-on, bump `version` in `apexsight-push-bridge/config.yaml`,
add a `CHANGELOG.md` entry, and push — Home Assistant will offer the update.

The store branding is included: `apexsight-push-bridge/icon.png` (256×256) and
`logo.png` (760×256) ship with the add-on, so HA shows the ApexSight mark
automatically.

See `apexsight-push-bridge/README.md` for full details on how the bridge works.
