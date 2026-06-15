"""Runtime configuration for the ApexSight push relay.

Secrets (the APNs .p8 key, Key ID) are NEVER read from the repo — they are
uploaded through the admin web GUI at runtime and stored in the data volume
(SQLite). Only non-secret defaults + the admin password come from the
environment. See .env.example.
"""
import os
import secrets
from pathlib import Path

# Where the SQLite DB + generated session secret live. Mounted as a Docker
# volume so uploaded keys and registrations survive container restarts.
DATA_DIR = Path(os.environ.get("APEX_DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "relay.db"

# Username + password for the admin web GUI. If the password is unset, the admin
# UI is locked entirely (the public /v1 API still works).
ADMIN_USERNAME = os.environ.get("APEX_ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.environ.get("APEX_ADMIN_PASSWORD", "").strip()

# Non-secret defaults — pre-filled in the GUI, editable there. The bundle id
# and team id are already public (they ship in the app), so defaulting them is
# safe and saves you typing.
DEFAULT_BUNDLE_ID = os.environ.get("APEX_BUNDLE_ID", "com.brandontoth.apexsight.native").strip()
DEFAULT_TEAM_ID = os.environ.get("APEX_TEAM_ID", "3Q9ZUDN4QZ").strip()

# Max /v1/notify + /v1/register calls accepted per client IP per minute.
RATE_LIMIT_PER_MINUTE = int(os.environ.get("APEX_RATE_LIMIT", "120"))


def session_secret() -> str:
    """A stable secret for signing admin session cookies.

    Read from APEX_SECRET_KEY if provided; otherwise generated once and
    persisted to the data volume so logins survive restarts.
    """
    env = os.environ.get("APEX_SECRET_KEY", "").strip()
    if env:
        return env
    path = DATA_DIR / "session.secret"
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_hex(32)
    path.write_text(value)
    path.chmod(0o600)
    return value
