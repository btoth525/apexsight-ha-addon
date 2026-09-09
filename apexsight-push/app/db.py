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
                device_name  TEXT,
                updated_at   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_voip_pairing ON voip_tokens(pairing_code);
            -- Every doorbell ring, one row PER PHONE, with what APNs actually said.
            --
            -- Written because the 2026-09-08 miss could not be diagnosed from what we kept: the
            -- only trace was one aggregate line, `ring -> 2 phones (failed 0)`, in a 100-line
            -- rolling buffer. That cannot answer "which phone", "was it accepted", or "was this
            -- token even the phone I think it is". `apns_id` is the identifier Apple's own
            -- delivery logs are keyed on, so a ring can now be chased all the way to Apple.
            CREATE TABLE IF NOT EXISTS ring_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                pairing_code TEXT NOT NULL,
                ts           REAL NOT NULL,
                device_name  TEXT,
                token_tail   TEXT,
                ok           INTEGER NOT NULL,
                detail       TEXT,
                apns_id      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ring_ts ON ring_log(pairing_code, ts);
            -- App-side diagnostics: the phone's own error log, shipped here so a problem seen
            -- while testing can be read back afterwards instead of being lost with the app.
            -- Deliberately dumb and append-only; `prune_diag` keeps it from growing without bound.
            CREATE TABLE IF NOT EXISTS diag (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                pairing_code TEXT NOT NULL,
                device       TEXT,
                build        TEXT,
                ts           REAL NOT NULL,
                level        TEXT NOT NULL,
                category     TEXT,
                message      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_diag_ts ON diag(pairing_code, ts);
            """
        )
        # Migration for DBs created before device_name existed (v1.7.0). ADD COLUMN is a
        # no-op on fresh installs (CREATE TABLE already has it), so swallow the dupe error.
        try:
            c.execute("ALTER TABLE devices ADD COLUMN device_name TEXT")
        except sqlite3.OperationalError:
            pass
        # Same, for VoIP tokens (v1.27.0). Without a name, `sent 2` is unattributable: there was
        # no way to tell whose phone a token belonged to, or that one of them was stale.
        try:
            c.execute("ALTER TABLE voip_tokens ADD COLUMN device_name TEXT")
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

def upsert_voip(voip_token: str, pairing_code: str, environment: str,
                device_name: str = "") -> None:
    """Register a phone's PushKit token. `device_name` is kept so a ring is attributable to a
    PHONE rather than to an opaque 64-hex string — a household with a stale or unexpected
    registration otherwise looks identical to one where every phone is fine."""
    with _conn() as c:
        c.execute(
            "INSERT INTO voip_tokens(voip_token, pairing_code, environment, device_name, updated_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(voip_token) DO UPDATE SET "
            "  pairing_code = excluded.pairing_code, "
            "  environment  = excluded.environment, "
            # An empty name must not blank a good one — older app builds don't send it at all.
            "  device_name  = COALESCE(NULLIF(excluded.device_name, ''), voip_tokens.device_name), "
            "  updated_at   = excluded.updated_at",
            (voip_token, pairing_code, environment, (device_name or "").strip()[:64],
             int(time.time())),
        )


def voip_tokens_for(pairing_code: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT voip_token, environment, device_name, updated_at "
            "FROM voip_tokens WHERE pairing_code = ?",
            (pairing_code,),
        ).fetchall()


# ---- ring log: what happened to each phone on each doorbell press ------------

# A doorbell is pressed a handful of times a day; 500 rows is months of history and a trivial
# amount of disk. Bounded anyway, on the same principle as the diag log: an aid that can grow
# without limit eventually becomes the outage.
RING_LOG_MAX_ROWS = 500


def insert_ring(pairing_code: str, device_name: str, token_tail: str,
                ok: bool, detail: str, apns_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO ring_log(pairing_code, ts, device_name, token_tail, ok, detail, apns_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (pairing_code, time.time(), (device_name or "")[:64], token_tail,
             1 if ok else 0, (detail or "")[:200], (apns_id or "")[:64]),
        )
        c.execute(
            "DELETE FROM ring_log WHERE pairing_code = ? AND id NOT IN ("
            "  SELECT id FROM ring_log WHERE pairing_code = ? ORDER BY id DESC LIMIT ?)",
            (pairing_code, pairing_code, RING_LOG_MAX_ROWS),
        )


def rings_for(pairing_code: str, limit: int = 50) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT ts, device_name, token_tail, ok, detail, apns_id FROM ring_log "
            "WHERE pairing_code = ? ORDER BY id DESC LIMIT ?",
            (pairing_code, max(1, min(int(limit), 500))),
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

# ---- app diagnostics -------------------------------------------------------
# The phone's own log, so "it did something weird while I was testing" survives long enough to be
# read. Capped hard: this is a debugging aid on a home server, never a reason to fill the disk.
DIAG_MAX_ROWS = 20_000


def insert_diag(pairing_code: str, device: str, build: str, entries: list[dict]) -> int:
    """Append log lines. Returns how many landed. Never raises on a bad row — a malformed
    diagnostic must not 500 the endpoint the app is trying to report a problem through."""
    rows = []
    for e in entries:
        try:
            rows.append((
                pairing_code,
                (device or "")[:64],
                (build or "")[:32],
                float(e.get("ts") or time.time()),
                str(e.get("level") or "info")[:12],
                str(e.get("category") or "")[:40],
                str(e.get("message") or "")[:2000],
            ))
        except (TypeError, ValueError):
            continue
    if not rows:
        return 0
    with _conn() as c:
        c.executemany(
            "INSERT INTO diag (pairing_code, device, build, ts, level, category, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def recent_diag(pairing_code: str, limit: int = 500, since_ts: float = 0.0,
                level: Optional[str] = None) -> list[sqlite3.Row]:
    q = ("SELECT id, device, build, ts, level, category, message FROM diag "
         "WHERE pairing_code = ? AND ts >= ?")
    args: list = [pairing_code, since_ts]
    if level:
        q += " AND level = ?"
        args.append(level)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(max(1, min(int(limit), 5000)))
    with _conn() as c:
        return c.execute(q, tuple(args)).fetchall()


def prune_diag(max_rows: int = DIAG_MAX_ROWS) -> int:
    """Keep only the newest `max_rows`. Returns how many were dropped."""
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM diag").fetchone()[0]
        if total <= max_rows:
            return 0
        c.execute(
            "DELETE FROM diag WHERE id NOT IN "
            "(SELECT id FROM diag ORDER BY id DESC LIMIT ?)",
            (max_rows,),
        )
        return total - max_rows


def clear_diag(pairing_code: str) -> int:
    with _conn() as c:
        cur = c.execute("DELETE FROM diag WHERE pairing_code = ?", (pairing_code,))
        return cur.rowcount or 0
