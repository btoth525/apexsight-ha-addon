# ApexSight — Home Assistant Add-on Repository

This is the source for the **ApexSight Push Bridge** Home Assistant add-on, which
forwards your Frigate alerts to the ApexSight push relay so your iPhone gets
**instant notifications even when the app is closed**. The add-on holds **no Apple
secrets** — only your relay URL and a pairing code.

It is published as a dedicated add-on repository at
**https://github.com/btoth525/apexsight-ha-addon** (Home Assistant discovers
add-on folders at the *root* of a repo, so it lives in its own repo).

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
