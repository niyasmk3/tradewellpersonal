#!/usr/bin/env python3
"""Backfill MCX GOLD history into the gold research DB.

The gold module lives entirely in recorder/mcx/ — its own data file, its own
docs, zero contact with the upstream app or the NIFTY evidence stream. It
reuses the same Kite credentials and the same chunked-fetch helpers as the
main backfill (one login a day powers everything).

Symbols stored:  GOLD_FUT (big contract), GOLDM_FUT (mini) — day candles
continuous ~5y, 3m/15m for the current contract's traded life.

Run any day after a Kite login:
  backend/.venv/bin/python recorder/mcx/backfill_mcx.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backfill import (IST, fetch_chunked, find_access_token, log,  # noqa: E402
                      read_env_key, save)

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "mcx_history.db"

CONTRACTS = {"GOLD": "GOLD_FUT", "GOLDM": "GOLDM_FUT"}
INTRADAY_PULLS = [("3minute", "3m", 90), ("15minute", "15m", 180)]


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS candles (
             symbol TEXT NOT NULL, tf TEXT NOT NULL, bar_ts INTEGER NOT NULL,
             open REAL, high REAL, low REAL, close REAL, volume REAL,
             PRIMARY KEY (symbol, tf, bar_ts))"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS candle_oi (
             symbol TEXT NOT NULL, tf TEXT NOT NULL, bar_ts INTEGER NOT NULL,
             oi REAL, PRIMARY KEY (symbol, tf, bar_ts))"""
    )
    return db


def main() -> int:
    api_key = read_env_key("KITE_API_KEY")
    token = find_access_token()
    if not api_key or not token:
        log("missing KITE_API_KEY or access token — log in once today, rerun")
        return 1

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)

    log("loading MCX instrument dump…")
    try:
        mcx = kite.instruments("MCX")
    except Exception as e:
        log(f"instruments fetch failed — token may be stale: {e}")
        return 1

    db = open_db()
    total = 0
    for name, sym in CONTRACTS.items():
        futs = sorted(
            (i for i in mcx
             if i.get("name") == name and i.get("instrument_type") == "FUT"),
            key=lambda i: i.get("expiry") or datetime.max,
        )
        if not futs:
            log(f"{name}: no future in MCX dump — skipped")
            continue
        tok = futs[0]["instrument_token"]
        log(f"{name}: current contract {futs[0].get('tradingsymbol')} "
            f"(expiry {futs[0].get('expiry')})")
        for interval, tf, chunk in INTRADAY_PULLS:
            n = save(db, sym, tf, fetch_chunked(
                kite, tok, interval, 120, chunk, False, True))
            total += n
            log(f"{sym} {tf}: {n:,} bars")
        n = save(db, sym, "day", fetch_chunked(
            kite, tok, "day", 1800, 1800, True, True))
        total += n
        log(f"{sym} day: {n:,} bars")

    log(f"MCX backfill done — {total:,} bars, db "
        f"{DB_PATH.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
