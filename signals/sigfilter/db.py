"""SQLite storage. One file, no server, survives restarts."""

import json
import sqlite3
import time
from contextlib import contextmanager

from .config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER NOT NULL,
    channel_name  TEXT,
    msg_id        INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    symbol        TEXT,
    side          TEXT,
    entry         REAL,
    sl            REAL,
    tp1           REAL,
    tps           TEXT,
    leverage      INTEGER,
    asset_class   TEXT,
    rr            REAL,
    score         REAL,
    verdict       TEXT,
    reasons       TEXT,
    breakdown     TEXT,
    fingerprint   TEXT,
    text          TEXT,
    forwarded     INTEGER DEFAULT 0,
    outcome       TEXT,
    outcome_ts    INTEGER,
    UNIQUE(channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_signals_outcome  ON signals(outcome, ts);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _migrate(conn):
    """Add columns introduced after a database was first created."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(signals)")}
    for column, ddl in (("breakdown", "TEXT"),):
        if column not in existing:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {ddl}")


@contextmanager
def connect(path=None):
    conn = sqlite3.connect(path or db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key, value):
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def bump_counter(conn, key, by=1):
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT)",
        (key, str(by), by),
    )


def get_counter(conn, key):
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def already_seen(conn, channel_id, msg_id):
    row = conn.execute(
        "SELECT 1 FROM signals WHERE channel_id = ? AND msg_id = ?", (channel_id, msg_id)
    ).fetchone()
    return row is not None


def record(conn, sig, channel_id, channel_name, msg_id, ts, score, verdict, reasons,
           fingerprint, breakdown=None):
    conn.execute(
        """INSERT OR IGNORE INTO signals
           (channel_id, channel_name, msg_id, ts, symbol, side, entry, sl, tp1, tps,
            leverage, asset_class, rr, score, verdict, reasons, breakdown, fingerprint, text)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            channel_id, channel_name, msg_id, ts, sig.symbol, sig.side, sig.entry, sig.sl,
            sig.tp1, json.dumps(sig.tps), sig.leverage, sig.asset_class, sig.risk_reward(),
            score, verdict, json.dumps(reasons), json.dumps(breakdown or {}),
            fingerprint, (sig.raw or "")[:4000],
        ),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def mark_forwarded(conn, signal_id):
    conn.execute("UPDATE signals SET forwarded = 1 WHERE id = ?", (signal_id,))


def recent_for_symbol(conn, symbol, side, since_ts, exclude_channel=None):
    query = "SELECT * FROM signals WHERE symbol = ? AND ts >= ?"
    params = [symbol, since_ts]
    if side:
        query += " AND side IS NOT NULL"
    if exclude_channel is not None:
        query += " AND channel_id != ?"
        params.append(exclude_channel)
    return conn.execute(query + " ORDER BY ts DESC LIMIT 50", params).fetchall()


def forwarded_today(conn, now=None):
    now = now or int(time.time())
    day_start = now - (now % 86400)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM signals WHERE forwarded = 1 AND ts >= ?", (day_start,)
    ).fetchone()
    return row["n"]


def channel_record(conn, channel_id):
    """(wins, losses) from graded outcomes only."""
    row = conn.execute(
        """SELECT SUM(outcome = 'WIN')  AS wins,
                  SUM(outcome = 'LOSS') AS losses
             FROM signals WHERE channel_id = ? AND outcome IN ('WIN','LOSS')""",
        (channel_id,),
    ).fetchone()
    return int(row["wins"] or 0), int(row["losses"] or 0)


def pending_outcomes(conn, horizon_hours, now=None):
    now = now or int(time.time())
    # Every asset class we have a free price source for - not crypto alone, or the
    # gold and forex channels that dominate most feeds would never be graded.
    return conn.execute(
        """SELECT * FROM signals
            WHERE outcome IS NULL AND entry IS NOT NULL AND sl IS NOT NULL
              AND tp1 IS NOT NULL
              AND asset_class IN ('crypto','metal','forex','index')
              AND ts <= ? ORDER BY ts ASC LIMIT 400""",
        (now - 300,),
    ).fetchall()


def reset_soft_outcomes(conn):
    """Clear EXPIRED/NODATA back to pending so a longer horizon or a recovered
    price source can try them again. WIN/LOSS are final and never touched."""
    before = conn.total_changes
    conn.execute("UPDATE signals SET outcome = NULL, outcome_ts = NULL "
                 "WHERE outcome IN ('EXPIRED', 'NODATA')")
    return conn.total_changes - before


def set_outcome(conn, signal_id, outcome, when=None):
    conn.execute(
        "UPDATE signals SET outcome = ?, outcome_ts = ? WHERE id = ?",
        (outcome, when or int(time.time()), signal_id),
    )
