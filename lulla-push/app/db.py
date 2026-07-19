"""SQLite sync store for Lulla.

The server is the ordering authority for a *household* (a pairing code). It holds the
canonical record log; phones push local changes and pull everything since their cursor.

Conflict rule mirrors the client's `ConflictResolver` EXACTLY so both sides agree:
  last-writer-wins arbitrated by `updated_at`, tie broken by `created_by` (lexicographic).
`server_seq` is a per-household monotonic counter used ONLY as the pull cursor — never as
the conflict arbiter (a delayed offline push must not clobber a newer edit just because it
arrived later). See docs/DECISIONS.md D-003.

Records are generic over `type` (LogEvent, Child, GrowthEntry, …) so every model syncs
through one path, mirroring the single-timeline design of plan §2.4.
"""
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from . import config


def init() -> None:
    config.ensure_data_dir()
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                household     TEXT NOT NULL,
                type          TEXT NOT NULL,
                id            TEXT NOT NULL,
                updated_at    REAL NOT NULL,
                created_by    TEXT NOT NULL DEFAULT '',
                is_tombstoned INTEGER NOT NULL DEFAULT 0,
                payload       TEXT NOT NULL,
                server_seq    INTEGER NOT NULL,
                PRIMARY KEY (household, type, id)
            );
            CREATE INDEX IF NOT EXISTS idx_records_pull ON records(household, server_seq);

            -- Per-household monotonic sequence (the pull cursor space).
            CREATE TABLE IF NOT EXISTS seq (
                household TEXT PRIMARY KEY,
                value     INTEGER NOT NULL DEFAULT 0
            );

            -- A phone that has joined a household via the pairing code.
            CREATE TABLE IF NOT EXISTS devices (
                token      TEXT PRIMARY KEY,
                household  TEXT NOT NULL,
                device_id  TEXT NOT NULL,
                name       TEXT,
                created_at REAL NOT NULL,
                last_seen  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_devices_household ON devices(household);

            -- ---- push / eventing (Phase 6.5, plan §7) --------------------------------

            -- Generic key/value config: APNs .p8 PEM, Key ID, Team ID, Bundle ID,
            -- env mode — set via the admin GUI, persisted in /data, NEVER in the repo.
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- APNs push targets. Named `push_devices` (NOT `devices`) so the sync auth
            -- table above is untouched. env is tracked PER token (§7.3) — the #1 delivery
            -- bug is mixing sandbox/prod.
            CREATE TABLE IF NOT EXISTS push_devices (
                device_token        TEXT PRIMARY KEY,
                household           TEXT NOT NULL DEFAULT '',
                parent_id           TEXT NOT NULL DEFAULT '',
                env                 TEXT NOT NULL DEFAULT 'prod',
                push_to_start_token TEXT,
                app_version         TEXT,
                last_seen           REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_push_devices_household ON push_devices(household);

            -- Per-activity Live Activity update tokens (from Activity.pushTokenUpdates).
            CREATE TABLE IF NOT EXISTS activities (
                activity_id TEXT PRIMARY KEY,
                child_id    TEXT NOT NULL DEFAULT '',
                kind        TEXT NOT NULL DEFAULT '',
                push_token  TEXT NOT NULL,
                env         TEXT NOT NULL DEFAULT 'prod',
                started_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activities_child ON activities(child_id);

            -- Delivery log for the admin dashboard + the watchdog's undeliverable signal.
            CREATE TABLE IF NOT EXISTS deliveries (
                ts          REAL NOT NULL,
                event       TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                reason      TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_deliveries_ts ON deliveries(ts);

            -- Monitoring heartbeat state (supervised loop, §7.7). Single row per key.
            CREATE TABLE IF NOT EXISTS monitoring (
                key   TEXT PRIMARY KEY,
                value REAL NOT NULL
            );
            """
        )


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    # Serialize writers so the seq counter can't race under concurrent pushes.
    conn.execute("PRAGMA busy_timeout = 5000;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---- device registration / auth --------------------------------------------

def register_device(household: str, device_id: str, name: Optional[str]) -> str:
    """Idempotent per (household, device_id): re-registering returns a fresh token but
    keeps the device identity stable."""
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _conn() as c:
        c.execute("DELETE FROM devices WHERE household=? AND device_id=?", (household, device_id))
        c.execute(
            "INSERT INTO devices(token, household, device_id, name, created_at, last_seen) VALUES(?,?,?,?,?,?)",
            (token, household, device_id, name, now, now),
        )
    return token


def resolve_token(token: str) -> Optional[sqlite3.Row]:
    with _conn() as c:
        row = c.execute("SELECT * FROM devices WHERE token=?", (token,)).fetchone()
        if row:
            c.execute("UPDATE devices SET last_seen=? WHERE token=?", (time.time(), token))
        return row


# ---- the sync core ----------------------------------------------------------

def _next_seq(c: sqlite3.Connection, household: str) -> int:
    c.execute(
        "INSERT INTO seq(household, value) VALUES(?, 1) "
        "ON CONFLICT(household) DO UPDATE SET value = value + 1",
        (household,),
    )
    return c.execute("SELECT value FROM seq WHERE household=?", (household,)).fetchone()[0]


def _incoming_wins(existing: sqlite3.Row, in_updated_at: float, in_created_by: str) -> bool:
    """LWW, arbiter = updated_at then created_by. Identical to Swift ConflictResolver."""
    if in_updated_at > existing["updated_at"]:
        return True
    if in_updated_at < existing["updated_at"]:
        return False
    return in_created_by > existing["created_by"]


def upsert(household: str, type_: str, id_: str, updated_at: float, created_by: str,
           is_tombstoned: bool, payload: str) -> dict:
    """Apply one incoming record with dedupe (by household,type,id) + LWW.
    Returns {applied: bool, server_seq: int}."""
    with _conn() as c:
        existing = c.execute(
            "SELECT * FROM records WHERE household=? AND type=? AND id=?",
            (household, type_, id_),
        ).fetchone()
        if existing is not None and not _incoming_wins(existing, updated_at, created_by):
            return {"applied": False, "server_seq": existing["server_seq"]}
        seq = _next_seq(c, household)
        c.execute(
            """
            INSERT INTO records(household, type, id, updated_at, created_by, is_tombstoned, payload, server_seq)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(household, type, id) DO UPDATE SET
                updated_at=excluded.updated_at,
                created_by=excluded.created_by,
                is_tombstoned=excluded.is_tombstoned,
                payload=excluded.payload,
                server_seq=excluded.server_seq
            """,
            (household, type_, id_, updated_at, created_by, 1 if is_tombstoned else 0, payload, seq),
        )
        return {"applied": True, "server_seq": seq}


def pull(household: str, since: int, limit: int = 500) -> dict:
    """Records changed since cursor `since`, ordered by server_seq. Returns
    {records: [...], cursor: int} where cursor is the highest server_seq returned
    (== `since` if nothing new)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT type, id, updated_at, created_by, is_tombstoned, payload, server_seq "
            "FROM records WHERE household=? AND server_seq > ? ORDER BY server_seq ASC LIMIT ?",
            (household, since, limit),
        ).fetchall()
    records = [
        {
            "type": r["type"],
            "id": r["id"],
            "updated_at": r["updated_at"],
            "created_by": r["created_by"],
            "is_tombstoned": bool(r["is_tombstoned"]),
            "payload": r["payload"],
            "server_seq": r["server_seq"],
        }
        for r in rows
    ]
    cursor = records[-1]["server_seq"] if records else since
    return {"records": records, "cursor": cursor}


def state(household: str) -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM records WHERE household=?", (household,)).fetchone()[0]
        live = c.execute(
            "SELECT COUNT(*) FROM records WHERE household=? AND is_tombstoned=0", (household,)
        ).fetchone()[0]
        devices = c.execute("SELECT COUNT(*) FROM devices WHERE household=?", (household,)).fetchone()[0]
        seq = c.execute("SELECT value FROM seq WHERE household=?", (household,)).fetchone()
    return {"records": total, "live": live, "devices": devices, "cursor": seq[0] if seq else 0}


def global_stats() -> dict:
    """Totals across all households — for the liveness probe / durability check."""
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        devices = c.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        households = c.execute("SELECT COUNT(DISTINCT household) FROM records").fetchone()[0]
    return {"records": total, "devices": devices, "households": households}


# ---- config store (APNs secrets in /data) -----------------------------------

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO config(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ---- push device registry ---------------------------------------------------

def upsert_push_device(
    *,
    device_token: str,
    household: str = "",
    parent_id: str = "",
    env: str = "prod",
    push_to_start_token: Optional[str] = None,
    app_version: Optional[str] = None,
) -> None:
    """Idempotent per device_token. Registering again refreshes env / tokens / last_seen."""
    now = time.time()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO push_devices(device_token, household, parent_id, env,
                                     push_to_start_token, app_version, last_seen)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(device_token) DO UPDATE SET
                household=excluded.household,
                parent_id=excluded.parent_id,
                env=excluded.env,
                push_to_start_token=excluded.push_to_start_token,
                app_version=excluded.app_version,
                last_seen=excluded.last_seen
            """,
            (device_token, household, parent_id, env, push_to_start_token, app_version, now),
        )


def push_devices(household: Optional[str] = None) -> list[sqlite3.Row]:
    with _conn() as c:
        if household:
            return c.execute(
                "SELECT * FROM push_devices WHERE household=?", (household,)
            ).fetchall()
        return c.execute("SELECT * FROM push_devices").fetchall()


def delete_push_device(device_token: str) -> None:
    """Prune a token APNs reported as 410 Gone / dead."""
    with _conn() as c:
        c.execute("DELETE FROM push_devices WHERE device_token=?", (device_token,))


# ---- Live Activity registry -------------------------------------------------

def register_activity(activity_id: str, child_id: str, kind: str, push_token: str,
                      env: str = "prod") -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO activities(activity_id, child_id, kind, push_token, env, started_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(activity_id) DO UPDATE SET
                child_id=excluded.child_id,
                kind=excluded.kind,
                push_token=excluded.push_token,
                env=excluded.env
            """,
            (activity_id, child_id, kind, push_token, env, now),
        )


def activities_for(activity_id: str = "", child_id: str = "") -> list[sqlite3.Row]:
    with _conn() as c:
        if activity_id:
            return c.execute(
                "SELECT * FROM activities WHERE activity_id=?", (activity_id,)
            ).fetchall()
        if child_id:
            return c.execute(
                "SELECT * FROM activities WHERE child_id=?", (child_id,)
            ).fetchall()
        return []


def delete_activity(activity_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM activities WHERE activity_id=?", (activity_id,))


# ---- delivery log -----------------------------------------------------------

def log_delivery(event: str, status_code: int, reason: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO deliveries(ts, event, status_code, reason) VALUES(?,?,?,?)",
            (time.time(), event, status_code, reason),
        )


def recent_deliveries(limit: int = 50) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM deliveries ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()


# ---- monitoring heartbeat (supervised loop, §7.7) ---------------------------

def set_monitoring(key: str, value: float) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO monitoring(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_monitoring(key: str) -> Optional[float]:
    with _conn() as c:
        row = c.execute("SELECT value FROM monitoring WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
