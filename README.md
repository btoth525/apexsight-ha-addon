# ApexSight — Home Assistant Add-on Repository

One add-on that gives your iPhone **instant Frigate notifications even when the
ApexSight app is closed** — built for Home Assistant OS.

**ApexSight Push** is all-in-one: it runs the APNs **relay** (holds your Apple
`.p8`, signs the pushes, has a web GUI via the **OPEN WEB UI** button) **and** the
Frigate **bridge** (forwards alerts) in a single container. No separate Docker
host, nothing else to install.

Published at **https://github.com/btoth525/apexsight-ha-addon**.

## Install

1. HA → **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add the URL above.
2. Install **ApexSight Push**.
3. **Configuration** → set admin username/password, your Frigate URL, leave the
   pairing code default. **Start**.
4. **OPEN WEB UI** → sign in → **Settings** → upload your `.p8` + Key/Team IDs.
5. Point a Cloudflare Tunnel / reverse proxy at `http://<HA-ip>:3421` (path empty).

Full details in `apexsight-push/README.md`.

## Publish / update (maintainer)

The canonical source lives in the main app repo under `homeassistant-addon/`.
To push it to this dedicated add-on repo:

```bash
git clone https://github.com/btoth525/apexsight-ha-addon
cd apexsight-ha-addon
cp -r /path/to/ApexSight/homeassistant-addon/. .
git add -A
git commit -m "ApexSight Push"
git push
```

When you change the add-on, bump `version` in `apexsight-push/config.yaml` so
Home Assistant offers the update.
