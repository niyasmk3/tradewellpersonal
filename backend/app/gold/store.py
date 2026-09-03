"""Candle stores + results JSON for the Gold module.

TWO SQLite files on purpose, split by replaceability:

  * .gold_candles.db — MCX candles from Kite. The 3m intraday history rides a
    ~120-day contract-listing window and is DELETED upstream as contracts roll,
    so this file is irreplaceable evidence (it joins the nightly state backup
    in app/ops.py).
  * .gold_xau.db — XAUUSD 1-minute bars from Dukascopy's public datafeed:
    ~1.3M rows, fully refetchable, deliberately NOT backed up. Its `days`
    table makes the backfill resume-safe — a day already recorded (stored,
    404-missing, or sanity-rejected) is never fetched again.

Both live in backend/ alongside the other runtime state and are gitignored.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_BASE = Path(__file__).resolve().parents[2]
MCX_DB_PATH = _BASE / ".gold_candles.db"
XAU_DB_PATH = _BASE / ".gold_xau.db"
RESULTS_PATH = _BASE / ".gold_results.json"

_MCX_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol    TEXT NOT NULL,       -- e.g. GOLDM
    timeframe TEXT NOT NULL,       -- '3m' (current contract) or 'day' (continuous)
    ts        INTEGER NOT NULL,    -- epoch seconds (bar open, IST market time)
    open      REAL NOT NULL,
    high      REAL NOT NULL,
    low       REAL NOT NULL,
    close     REAL NOT NULL,
    volume    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_XAU_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    ts     INTEGER PRIMARY KEY,    -- epoch seconds UTC (bar open)
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS days (
    day    TEXT PRIMARY KEY,       -- UTC date 'YYYY-MM-DD'
    status TEXT NOT NULL,          -- ok | missing | rejected
    bars   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _mcx() -> sqlite3.Connection:
    conn = sqlite3.connect(MCX_DB_PATH)
    conn.executescript(_MCX_SCHEMA)
    return conn


def _xau() -> sqlite3.Connection:
    conn = sqlite3.connect(XAU_DB_PATH)
    conn.executescript(_XAU_SCHEMA)
    return conn


# ---- MCX ---------------------------------------------------------------------

def upsert_mcx(symbol: str, timeframe: str, rows: list) -> int:
    """rows: iterable of (ts, open, high, low, close, volume). Returns rows written."""
    if not rows:
        return 0
    with _mcx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO candles "
            "(symbol, timeframe, ts, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(symbol, timeframe, r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows],
        )
    return len(rows)


def mcx_last_ts(symbol: str, timeframe: str) -> Optional[int]:
    with _mcx() as conn:
        row = conn.execute(
            "SELECT MAX(ts) FROM candles WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def mcx_count(symbol: str, timeframe: str) -> int:
    with _mcx() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM candles WHERE symbol = ? AND timeframe = ?",
            (symbol, timeframe)).fetchone()[0])


def load_mcx(symbol: str, timeframe: str) -> pd.DataFrame:
    """All candles for one (symbol, timeframe) as a DataFrame sorted by ts."""
    with _mcx() as conn:
        return pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE symbol = ? AND timeframe = ? ORDER BY ts",
            conn, params=(symbol, timeframe))


def mcx_prev_close(symbol: str, timeframe: str, before_ts: int) -> Optional[float]:
    """Close of the last bar strictly before `before_ts` — the live card's
    previous-session close, without loading the whole frame on a poll."""
    with _mcx() as conn:
        row = conn.execute(
            "SELECT close FROM candles WHERE symbol = ? AND timeframe = ? "
            "AND ts < ? ORDER BY ts DESC LIMIT 1",
            (symbol, timeframe, int(before_ts))).fetchone()
    return float(row[0]) if row else None


def set_meta(key: str, value: str) -> None:
    with _mcx() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_meta(key: str) -> Optional[str]:
    with _mcx() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


# ---- XAUUSD ------------------------------------------------------------------

def xau_write_day(day_iso: str, status: str, rows: list) -> int:
    """One UTC day, atomically: its bars (if any) plus the day-status row the
    resume logic keys on. rows: iterable of (ts, open, high, low, close, volume)."""
    with _xau() as conn:
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO candles (ts, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT OR REPLACE INTO days (day, status, bars) VALUES (?, ?, ?)",
                     (day_iso, status, len(rows)))
    return len(rows)


def xau_recorded_days() -> set:
    with _xau() as conn:
        return {r[0] for r in conn.execute("SELECT day FROM days")}


def xau_count() -> int:
    with _xau() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0])


def xau_day_counts() -> dict:
    with _xau() as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM days GROUP BY status").fetchall()
    return {status: int(n) for status, n in rows}


def load_xau() -> pd.DataFrame:
    with _xau() as conn:
        return pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume FROM candles ORDER BY ts", conn)


def xau_set_meta(key: str, value: str) -> None:
    with _xau() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def xau_get_meta(key: str) -> Optional[str]:
    with _xau() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


# ---- results -----------------------------------------------------------------

def save_results(results: dict) -> None:
    """tmp + rename, same as the closing/overnight stores: an analysis rewrite
    must never race a tab's poll into a half-written JSON read."""
    results["generated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = RESULTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=1, default=str))
    tmp.replace(RESULTS_PATH)


def load_results() -> Optional[dict]:
    if not RESULTS_PATH.exists():
        return None
    try:
        return json.loads(RESULTS_PATH.read_text())
    except Exception:
        return None
