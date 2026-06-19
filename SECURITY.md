# Security model — ApexSight Push

The relay is meant to be **reachable from the public internet** (your iPhone
talks to it through a Cloudflare Tunnel / reverse proxy). Treat it like any
internet-facing service. This document describes what protects what, and how to
expose it safely.

## What an attacker could reach

Port `3421` serves three things on the same host:

| Path            | Who calls it            | Protection |
|-----------------|-------------------------|------------|
| `/v1/*`         | iOS app + local bridge  | **Bearer API token** (`api_token`) + rate limit |
| `/healthz`      | tunnel / uptime checks  | Public, returns only `{"ok": true}` |
| `/admin/*`      | you, in a browser       | Username + password, brute-force lockout, strict CSP |

## The API token is the security boundary

Every state-changing call (`/v1/register`, `/v1/notify`, `/v1/gate`,
`/v1/style`, `/v1/recap`, `/v1/test`, `/v1/unregister`) and `/v1/status`
requires `Authorization: Bearer <api_token>`. Without it the API fails closed.

This matters because `/v1/notify` sends arbitrary push content and `/v1/gate`
can **disarm/silence your alerts** — so the pairing code alone (8 characters,
visible in the app, formerly shared by default) must **not** be the only thing
standing between an attacker and your notifications.

- Set `api_token` in the add-on options, or let the add-on generate one
  (persisted to `/data/api.token`, shown on the web GUI dashboard).
- Paste the same token into the ApexSight app.
- Rotate it by changing the option (or deleting `/data/api.token`) and
  restarting; update the app afterwards.

## Lock the origin to your tunnel

The real-client-IP logic (used for rate limiting and login lockout) trusts the
`CF-Connecting-IP` / `X-Forwarded-For` headers. Those are only trustworthy if
the **only** way to reach port `3421` is through your proxy. Therefore:

- Do **not** port-forward `3421` from your router to the internet.
- Reach it exclusively via the Cloudflare Tunnel (cloudflared dials *out*).
- Optionally put **Cloudflare Access** in front of `/admin` so the admin GUI is
  never exposed to anonymous traffic at all.

## Admin GUI

- Set a long `admin_password`. With it empty the admin UI is disabled entirely.
- 5 failed logins per IP → 15-minute lockout.
- Turn on `secure_cookies` once you reach the GUI only over HTTPS (the cookie
  then won't work over the plain-HTTP local "OPEN WEB UI" button — use the
  HTTPS hostname instead).
- Responses carry `Content-Security-Policy`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

## Secrets at rest

Your APNs `.p8`, Key ID, the generated API token and the session secret live in
the add-on's persistent `/data` volume (SQLite + files), never in this repo.
They are readable by anyone with shell access to the Home Assistant host —
that host is part of your trusted boundary.

## Reporting

Found a vulnerability? Email brandontoth525@gmail.com rather than opening a
public issue.
