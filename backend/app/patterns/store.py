"""SQLite candle store + results JSON for the Patterns Module.

SQLite (stdlib) instead of the project's usual JSON files: 3 years of 5-min
bars is ~55k rows — a JSON blob that size would be reparsed wholesale on every
analysis run, while SQLite gives incremental sync (INSERT OR REPLACE keyed on
ts) for free. Both files live in backend/ alongside the other runtime state
and are gitignored.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_BASE = Path(__file__).resolve().parents[2]
DB_PATH = _BASE / ".patterns_candles.db"
RESULTS_PATH = _BASE / ".patterns_results.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    ts     INTEGER PRIMARY KEY,   -- epoch seconds (bar open, IST market time)
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    vol_proxy REAL NOT NULL DEFAULT 0   -- NIFTYBEES 5-min volume (index has none)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def upsert_candles(rows: list) -> int:
    """rows: iterable of (ts, open, high, low, close). Returns rows written."""
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO candles (ts, open, high, low, close, vol_proxy) "
            "VALUES (?, ?, ?, ?, ?, COALESCE((SELECT vol_proxy FROM candles WHERE ts = ?), 0))",
            [(r[0], r[1], r[2], r[3], r[4], r[0]) for r in rows],
        )
    return len(rows)


def update_vol_proxy(pairs: list) -> int:
    """pairs: iterable of (ts, volume). Only updates bars that already exist —
    the index OHLC is the spine; proxy volume with no matching index bar is
    noise (ETF bars can exist in pre-open freezes the index doesn't print)."""
    if not pairs:
        return 0
    with _connect() as conn:
        cur = conn.executemany(
            "UPDATE candles SET vol_proxy = ? WHERE ts = ?",
            [(v, ts) for ts, v in pairs],
        )
        return cur.rowcount


def last_ts() -> Optional[int]:
    with _connect() as conn:
        row = conn.execute("SELECT MAX(ts) FROM candles").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def candle_count() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0])


def load_frame() -> pd.DataFrame:
    """All candles as a DataFrame sorted by ts. Columns: ts, open, high, low,
    close, vol_proxy."""
    with _connect() as conn:
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close, vol_proxy FROM candles ORDER BY ts", conn
        )
    return df


def load_tail(n: int) -> pd.DataFrame:
    """The last `n` candles, ascending — for callers that only need detector
    warm-up context. Pulling the full multi-year table to keep 60 rows made
    every live-read poll pay for the whole history (review catch)."""
    with _connect() as conn:
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close, vol_proxy FROM candles "
            "ORDER BY ts DESC LIMIT ?", conn, params=(int(n),)
        )
    return df.iloc[::-1].reset_index(drop=True)


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def save_results(results: dict) -> None:
    results["generated_at"] = datetime.now(timezone.utc).isoformat()
    RESULTS_PATH.write_text(json.dumps(results, indent=1, default=str))


def load_results() -> Optional[dict]:
    if not RESULTS_PATH.exists():
        return None
    try:
        return json.loads(RESULTS_PATH.read_text())
    except Exception:
        return None
