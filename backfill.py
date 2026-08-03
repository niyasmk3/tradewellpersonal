#!/usr/bin/env python3
"""Backfill PAST charts from Kite's historical API into the recorder DB.

The recorder captures live data going forward; this pulls what history Kite
still has — continuous futures candles (the series the signal engine runs on),
spot index and INDIA VIX dailies — so training features have years of context
instead of starting from day one. Intraday option-chain history does NOT exist
anywhere and cannot be backfilled; that dataset only grows via the recorder.

REQUIREMENTS: the Connect subscription (historical API) and one day's Kite
login — this script reads the api_key from backend/.env and the day's access
token from backend/.kite_session.json (the backend writes it on login), or
KITE_ACCESS_TOKEN if set. Run any evening after a login:

  backend/.venv/bin/python recorder/backfill.py            # 365 days
  backend/.venv/bin/python recorder/backfill.py --days 730

Stored into the same SQLite as the recorder, distinct symbols so live-built
bars are never overwritten:  NIFTY_FUT / BANKNIFTY_FUT / FINNIFTY_FUT
(3m + 15m + day, continuous), NIFTY_SPOT / BANKNIFTY_SPOT / FINNIFTY_SPOT
and INDIAVIX (day). Idempotent: re-runs upsert by (symbol, tf, bar_ts).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "backend" / ".env"
SESSION = ROOT / "backend" / ".kite_session.json"
DB_PATH = Path(__file__).resolve().parent / "data" / "tradewell_history.db"

IST = timezone(timedelta(hours=5, minutes=30))

FUTURES = {"NIFTY": "NIFTY_FUT", "BANKNIFTY": "BANKNIFTY_FUT", "FINNIFTY": "FINNIFTY_FUT"}
INDICES = {"NIFTY 50": "NIFTY_SPOT", "NIFTY BANK": "BANKNIFTY_SPOT",
           "NIFTY FIN SERVICE": "FINNIFTY_SPOT", "INDIA VIX": "INDIAVIX"}

# (kite interval, our tf tag, max days per request — conservative vs API caps)
INTRADAY_PULLS = [("3minute", "3m", 90), ("15minute", "15m", 180)]
DAY_PULL = ("day", "day", 1800)
REQUEST_GAP_S = 0.5  # historical API allows ~3 req/s; stay well under


def log(msg: str) -> None:
    print(f"{datetime.now(IST).strftime('%H:%M:%S')}  {msg}", flush=True)


def read_env_key(name: str) -> str | None:
    if not ENV.exists():
        return None
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}=") and not line.startswith("#"):
            v = line.split("=", 1)[1].strip()
            return v or None
    return None


def find_access_token() -> str | None:
    tok = read_env_key("KITE_ACCESS_TOKEN")
    if tok:
        return tok
    if SESSION.exists():
        try:
            blob = json.loads(SESSION.read_text())
        except json.JSONDecodeError:
            return None

        def dig(o: object) -> str | None:
            if isinstance(o, dict):
                if isinstance(o.get("access_token"), str):
                    return o["access_token"]
                for v in o.values():
                    got = dig(v)
                    if got:
                        return got
            return None

        return dig(blob)
    return None


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
    # OI per bar exists only in historical pulls (oi=1), so it lives in its own
    # column-light table rather than widening the recorder's candles schema.
    db.execute(
        """CREATE TABLE IF NOT EXISTS candle_oi (
             symbol TEXT NOT NULL, tf TEXT NOT NULL, bar_ts INTEGER NOT NULL,
             oi REAL, PRIMARY KEY (symbol, tf, bar_ts))"""
    )
    return db


def fetch_chunked(kite, token: int, interval: str, days: int, chunk_days: int,
                  continuous: bool, want_oi: bool):
    """Yield candle dicts across chunked requests, oldest first."""
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    cur = start
    while cur < end:
        stop = min(cur + timedelta(days=chunk_days), end)
        for attempt in (1, 2, 3):
            try:
                bars = kite.historical_data(
                    token, cur, stop, interval,
                    continuous=continuous, oi=want_oi,
                )
                yield from bars
                break
            except Exception as e:  # network/throttle — back off and retry
                if attempt == 3:
                    log(f"  chunk {cur:%d-%b-%Y}→{stop:%d-%b-%Y} failed after retries: {e}")
                else:
                    time.sleep(2 * attempt)
        time.sleep(REQUEST_GAP_S)
        cur = stop


def save(db: sqlite3.Connection, symbol: str, tf: str, bars) -> int:
    rows, oi_rows = [], []
    for b in bars:
        dt = b.get("date")
        if dt is None:
            continue
        ts = int(dt.timestamp())
        rows.append((symbol, tf, ts, b.get("open"), b.get("high"),
                     b.get("low"), b.get("close"), b.get("volume")))
        if b.get("oi") is not None:
            oi_rows.append((symbol, tf, ts, b.get("oi")))
    db.executemany(
        "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", rows)
    if oi_rows:
        db.executemany(
            "INSERT OR REPLACE INTO candle_oi VALUES (?,?,?,?)", oi_rows)
    db.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365,
                    help="how far back for intraday timeframes (day candles always pull ~5y)")
    args = ap.parse_args()

    api_key = read_env_key("KITE_API_KEY")
    token = find_access_token()
    if not api_key or api_key == "your_api_key_here":
        log("KITE_API_KEY missing in backend/.env — add your key first"); return 1
    if not token:
        log("no access token — log in via the dashboard once today, then rerun"); return 1

    from kiteconnect import KiteConnect  # backend venv dependency
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)

    log("loading instrument dumps (NFO + NSE)…")
    try:
        nfo = kite.instruments("NFO")
        nse = kite.instruments("NSE")
    except Exception as e:
        log(f"instruments fetch failed — token may be stale (login again): {e}")
        return 1

    db = open_db()
    total = 0

    # Nearest-expiry index futures, pulled with continuous=1 so the series
    # stitches through past contracts instead of stopping at this one's birth.
    for name, sym in FUTURES.items():
        futs = sorted(
            (i for i in nfo
             if i.get("name") == name and i.get("instrument_type") == "FUT"),
            key=lambda i: i.get("expiry") or datetime.max,
        )
        if not futs:
            log(f"{name}: no future found in NFO dump — skipped"); continue
        tok = futs[0]["instrument_token"]
        # Kite's continuous=1 stitching works for DAY candles only — intraday
        # intervals reject it ("invalid interval for continuous data", learned
        # 03-Aug). Intraday pulls therefore use the current contract directly,
        # which limits history to its listing (~3 months) — the practical max.
        for interval, tf, chunk in INTRADAY_PULLS:
            n = save(db, sym, tf, fetch_chunked(
                kite, tok, interval, min(args.days, 95), chunk, False, True))
            total += n
            log(f"{sym} {tf}: {n:,} bars")
        interval, tf, chunk = DAY_PULL
        n = save(db, sym, tf, fetch_chunked(
            kite, tok, interval, 1800, chunk, True, True))
        total += n
        log(f"{sym} day: {n:,} bars")

    # Spot indices + VIX: daily context series.
    idx = {i.get("tradingsymbol"): i for i in nse
           if i.get("segment") == "INDICES"}
    for ts_name, sym in INDICES.items():
        inst = idx.get(ts_name)
        if not inst:
            log(f"{ts_name}: not in NSE indices dump — skipped"); continue
        interval, tf, chunk = DAY_PULL
        n = save(db, sym, tf, fetch_chunked(
            kite, inst["instrument_token"], interval, 1800, chunk, False, False))
        total += n
        log(f"{sym} day: {n:,} bars")

    size_mb = DB_PATH.stat().st_size / 1e6
    log(f"backfill done — {total:,} bars total, db {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
