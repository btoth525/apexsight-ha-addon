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
                updated_at   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_devices_pairing ON devices(pairing_code);
            """
        )


@contextmanager
def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
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


def all_config() -> dict:
    with _conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM config")}


# ---- devices ----------------------------------------------------------------

def upsert_device(device_token: str, pairing_code: str, environment: str, platform: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO devices(device_token, pairing_code, environment, platform, updated_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(device_token) DO UPDATE SET "
            "  pairing_code = excluded.pairing_code, "
            "  environment  = excluded.environment, "
            "  platform     = excluded.platform, "
            "  updated_at   = excluded.updated_at",
            (device_token, pairing_code, environment, platform, int(time.time())),
        )


def delete_device(device_token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM devices WHERE device_token = ?", (device_token,))


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
            "SELECT device_token, pairing_code, environment, platform, updated_at "
            "FROM devices ORDER BY updated_at DESC"
        ).fetchall()


def device_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
