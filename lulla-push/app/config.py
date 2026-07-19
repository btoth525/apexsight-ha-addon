"""Runtime config for the Lulla Push + Sync relay.

Secrets and the SQLite store live under /data (the add-on's persistent volume).
Everything is env-driven by run.sh (which reads the HA add-on options).
"""
import os
import secrets

# Persistent volume (HA add-on convention). Overridable for local tests.
DATA_DIR = os.environ.get("LULLA_DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "lulla.db")

# Household pairing code — the shared secret both phones present to join the same
# data set. Default matches the plan's LULLA-<household> shape.
PAIRING_CODE = os.environ.get("PAIRING_CODE", "LULLA-TOTH-0001").upper().strip()

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Host port the add-on binds (Cloudflare Tunnel → lulla.plexserver525.com → here).
PORT = int(os.environ.get("PORT", "6969"))

# Routing policy (§7.4). Driven by the add-on options via run.sh.
QUIET_HOURS_START = os.environ.get("QUIET_HOURS_START", "22:00")
QUIET_HOURS_END = os.environ.get("QUIET_HOURS_END", "07:00")
NAP_AWARE = os.environ.get("NAP_AWARE", "true").lower() in ("1", "true", "yes", "on")

# APNs push topic / bundle id used when signing (also stored in the DB config table via
# the admin GUI). The bundle id in the DB config wins at send time.
BUNDLE_ID = os.environ.get("BUNDLE_ID", "")

# TEST-ONLY: when set, register accepts ANY presented pairing code and treats it as the
# household, so the live test suite can isolate each test in its own household. Production
# leaves this unset → only the configured PAIRING_CODE is accepted.
ACCEPT_ANY_PAIRING = os.environ.get("LULLA_ACCEPT_ANY_PAIRING") == "1"

_SESSION_SECRET = None


def session_secret() -> str:
    global _SESSION_SECRET
    if _SESSION_SECRET is None:
        # Stable across a process; regenerated per boot is fine for the admin session.
        _SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
    return _SESSION_SECRET


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
