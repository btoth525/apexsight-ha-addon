"""Tiny SQLite layer for the relay.

Three concerns:
  * config   — key/value store for the uploaded APNs credentials + settings
  * devices  — every iOS device token, tied to its household pairing code
  * (pairings are implicit: a pairing code is just the set of devices sharing it)
"""
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from . import config


def init() -> None:
    with _conn() as c:
        # WAL lets the bridge process read while the relay writes (and vice versa) without the
        # "database is locked" errors a burst of alerts × several phones could otherwise surface as
        # unhandled 500s. Persists on the DB file, so the bridge's connections inherit it too.
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                device_token TEXT PRIMARY KEY,
                pairing_code TEXT NOT NULL,
                environment  TEXT NOT NULL DEFAULT 'production',
                platform     TEXT,
                device_name  TEXT,
                updated_at   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_devices_pairing ON devices(pairing_code);
            CREATE TABLE IF NOT EXISTS recap_events (
                pairing_code TEXT NOT NULL,
                event_id     TEXT NOT NULL,
                camera       TEXT,
                label        TEXT,
                sub_label    TEXT,
                ts           REAL NOT NULL,
                PRIMARY KEY (pairing_code, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_recap_ts ON recap_events(pairing_code, ts);
            CREATE TABLE IF NOT EXISTS voip_tokens (
                voip_token   TEXT PRIMARY KEY,
                pairing_code TEXT NOT NULL,
                environment  TEXT NOT NULL DEFAULT 'production',
                updated_at   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_voip_pairing ON voip_tokens(pairing_code);
            """
        )
        # Migration for DBs created before device_name existed (v1.7.0). ADD COLUMN is a
        # no-op on fresh installs (CREATE TABLE already has it), so swallow the dupe error.
        try:
            c.execute("ALTER TABLE devices ADD COLUMN device_name TEXT")
        except sqlite3.OperationalError:
            pass


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")   # wait for a competing writer instead of raising at once
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---- config key/value -------------------------------------------------------

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def next_mode_request_seq() -> int:
    """Atomically increment and return the monotonic mode-request seq in a SINGLE statement, so two
    concurrent /v1/set-mode calls (FastAPI runs the sync endpoint in a threadpool) can't both read
    the same value and then both write seq N+1 — a duplicate the bridge dedupes on, silently dropping
    one arm/disarm command."""
    with _conn() as c:
        row = c.execute(
            "INSERT INTO config(key, value) VALUES('mode_request_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
            "RETURNING value"
        ).fetchone()
    return int(row["value"])


def all_config() -> dict:
    with _conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM config")}


# ---- devices ----------------------------------------------------------------

def upsert_device(
    device_token: str,
    pairing_code: str,
    environment: str,
    platform: str = "",
    device_name: str = "",
) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO devices(device_token, pairing_code, environment, platform, device_name, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device_token) DO UPDATE SET "
            "  pairing_code = excluded.pairing_code, "
            "  environment  = excluded.environment, "
            "  platform     = excluded.platform, "
            # Preserve an existing name when this upsert carries none (e.g. a token
            # refresh re-registers before the app re-syncs the user-set name).
            "  device_name  = COALESCE(NULLIF(excluded.device_name, ''), devices.device_name), "
            "  updated_at   = excluded.updated_at",
            (device_token, pairing_code, environment, platform, device_name, int(time.time())),
        )


def set_device_name(device_token: str, device_name: str) -> None:
    """Update only the phone's display name (+ last-seen) for an already-registered device.
    Touches nothing else — so a name refresh from the foreground sync can never clobber the
    device's environment/pairing (that would break APNs delivery)."""
    with _conn() as c:
        c.execute(
            "UPDATE devices SET device_name = ?, updated_at = ? WHERE device_token = ?",
            (device_name, int(time.time()), device_token),
        )


def device_name_for(device_token: str) -> str:
    """The user-set friendly name for a device token, or '' — used to record WHO armed."""
    with _conn() as c:
        row = c.execute(
            "SELECT device_name FROM devices WHERE device_token = ?", (device_token,)
        ).fetchone()
        return (row["device_name"] or "") if row else ""


def delete_device(device_token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM devices WHERE device_token = ?", (device_token,))


def delete_device_if_unchanged(device_token: str, expected_updated_at) -> None:
    """Like delete_device, but only deletes the row if it hasn't been touched since the caller's
    read — for pruning a token APNs reported dead. Without this, a device that re-registers
    (`upsert_device`, bumping `updated_at`) between the read and the prune would have its FRESH
    registration silently wiped by a delete that was really only valid against the stale row."""
    with _conn() as c:
        c.execute(
            "DELETE FROM devices WHERE device_token = ? AND updated_at = ?",
            (device_token, expected_updated_at),
        )


def devices_for(pairing_code: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT device_token, environment, platform, updated_at FROM devices "
            "WHERE pairing_code = ?",
            (pairing_code,),
        ).fetchall()


def all_devices() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT device_token, pairing_code, environment, platform, device_name, updated_at "
            "FROM devices ORDER BY updated_at DESC"
        ).fetchall()


def device_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]


# ---- VoIP (PushKit) tokens — used to ring a phone via CallKit on a doorbell press ----

def upsert_voip(voip_token: str, pairing_code: str, environment: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO voip_tokens(voip_token, pairing_code, environment, updated_at) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(voip_token) DO UPDATE SET "
            "  pairing_code = excluded.pairing_code, "
            "  environment  = excluded.environment, "
            "  updated_at   = excluded.updated_at",
            (voip_token, pairing_code, environment, int(time.time())),
        )


def voip_tokens_for(pairing_code: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT voip_token, environment FROM voip_tokens WHERE pairing_code = ?",
            (pairing_code,),
        ).fetchall()


def delete_voip(voip_token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM voip_tokens WHERE voip_token = ?", (voip_token,))


# ---- recap events (accumulated from the MQTT stream by the bridge) -----------

def recap_events_between(pairing_code: str, start_ts: float, end_ts: float) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT camera, label, sub_label, ts FROM recap_events "
            "WHERE pairing_code = ? AND ts >= ? AND ts <= ?",
            (pairing_code, start_ts, end_ts),
        ).fetchall()


def prune_recap_events(before_ts: float) -> None:
    with _conn() as c:
        c.execute("DELETE FROM recap_events WHERE ts < ?", (before_ts,))
