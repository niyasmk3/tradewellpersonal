"""India VIX bar store + results JSON for the Closing Day Strategy module.

Why a second SQLite file rather than a column on the patterns candle table:
VIX is a different instrument on a different token with its own trading
calendar quirks (it prints on days the index does and vice versa, but the two
have drifted on special sessions). Keeping it in its own table means a failed
VIX sync can never corrupt the price spine that four other modules read.

The NIFTY 5-min spine itself is NOT duplicated here — app/patterns/store.py
already holds 3 years of it and this module reads that table read-only.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

_BASE = Path(__file__).resolve().parents[2]
DB_PATH = _BASE / ".closing_vix.db"
RESULTS_PATH = _BASE / ".closing_results.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vix (
    ts    INTEGER PRIMARY KEY,   -- epoch seconds (bar open, IST market time)
    open  REAL NOT NULL,
    high  REAL NOT NULL,
    low   REAL NOT NULL,
    close REAL NOT NULL
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


def upsert_vix(rows: list) -> int:
    """rows: iterable of (ts, open, high, low, close). Returns rows written."""
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO vix (ts, open, high, low, close) VALUES (?,?,?,?,?)",
            [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows],
        )
    return len(rows)


def last_vix_ts() -> Optional[int]:
    with _connect() as conn:
        row = conn.execute("SELECT MAX(ts) FROM vix").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def vix_count() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM vix").fetchone()[0])


def load_vix() -> pd.DataFrame:
    """All VIX bars ascending. Columns: ts, open, high, low, close."""
    with _connect() as conn:
        return pd.read_sql_query(
            "SELECT ts, open, high, low, close FROM vix ORDER BY ts", conn)


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
