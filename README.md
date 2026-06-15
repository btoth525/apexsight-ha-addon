# ApexSight — Home Assistant Add-on

This folder contains the **ApexSight Push Bridge** add-on, which forwards Frigate
alerts to your push relay. There are two ways to install it.

## Option 1 — Local install (fastest, for yourself)

Home Assistant scans a special `/addons` folder for "local" add-ons.

1. Get the files onto your HA machine, into `/addons/apexsight-push-bridge/`:
   - Easiest: install the **Samba share** or **Studio Code Server** / **File
     editor** add-on, then copy the `apexsight-push-bridge/` folder (all of it:
     `config.yaml`, `Dockerfile`, `build.yaml`, `run.sh`, `bridge.py`) into the
     `addons` share.
   - Or over SSH: `scp -r homeassistant-addon/apexsight-push-bridge \
     root@homeassistant:/addons/`
2. HA → **Settings → Add-ons → Add-on Store**, click the **⟳** (top-right) to
   refresh. **ApexSight Push Bridge** appears under **Local add-ons**.
3. Click it → **Install** → **Configuration** (fill in the options below) →
   **Start**.

## Option 2 — Add-on repository (for testers / one-click installs)

Home Assistant can install add-ons straight from a Git repo, but it looks for the
add-on folders **at the repo root**. So publish a small dedicated repo:

```
your-ha-addons-repo/
├── repository.yaml              ← copy from this folder
└── apexsight-push-bridge/       ← copy this whole folder
    ├── config.yaml
    ├── Dockerfile
    ├── build.yaml
    ├── run.sh
    └── bridge.py
```

1. Create a public GitHub repo (e.g. `apexsight-ha-addons`) with that layout
   (copy `repository.yaml` and the `apexsight-push-bridge/` folder from here to
   its root).
2. Testers: HA → **Settings → Add-ons → Add-on Store → ⋮ (top-right) →
   Repositories**, paste the repo URL, **Add**.
3. **ApexSight Push Bridge** now shows in the store → **Install → Configure → Start**.

## Configuration (both options)

| Option | Value |
|--------|-------|
| `relay_url` | Your relay, e.g. `https://push.yourdomain.com` |
| `pairing_code` | The code shown in ApexSight → Settings → Instant Push |
| `frigate_base_url` | URL your phone can reach Frigate at (for the snapshot/GIF) |
| `alerts_only` | `true` = only alerts; `false` = also detections |

MQTT is auto-filled from the Home Assistant broker; override `mqtt_*` only if you
run a separate broker.

See `apexsight-push-bridge/README.md` for full details.
