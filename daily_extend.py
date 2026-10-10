#!/usr/bin/env python3
"""Extend the deep-backfilled DAILY series to today from data we already hold.

Why: backfill.py needs a live Kite token (valid only on a login day), so the
official NIFTY_SPOT / BANKNIFTY_SPOT / FINNIFTY_SPOT / INDIAVIX dailies stop
on the last evening it was run (19-Aug-2026 as of 10-Oct). The features that
read them — vix_close, htf_ret_20d_pct — went silently stale for every later
card. This step derives the missing days from local sources every evening:

  index dailies  <- the recorder's own live 3m feed (NIFTY/BANKNIFTY/FINNIFTY)
  INDIAVIX daily <- the Closing lab's 5-minute VIX store
                    (backend/.closing_vix.db, opened read-only)

Only COMPLETE sessions are written (first bar by 09:20, last bar at/after
15:20 IST), only for days after the last stored daily bar, never over an
existing row. Provenance goes to derived_daily, so when backfill.py runs on
a login day its upsert replaces them with the official bars. Daily bar_ts
convention (matches backfill.py): IST midnight of the session.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = HERE / "data" / "tradewell_history.db"
VIX_DB = ROOT / "backend" / ".closing_vix.db"
IST = timezone(timedelta(hours=5, minutes=30))
LIVE_TO_DAILY = {"NIFTY": "NIFTY_SPOT", "BANKNIFTY": "BANKNIFTY_SPOT",
                 "FINNIFTY": "FINNIFTY_SPOT"}
FIRST_BAR_BY = 9 * 60 + 20      # minutes after IST midnight
LAST_BAR_FROM = 15 * 60 + 20


def _day(ts: int) -> int:
    return (int(ts) + 19800) // 86400


def _day_start(day: int) -> int:
    return day * 86400 - 19800


def _minute(ts: int) -> int:
    return ((int(ts) + 19800) % 86400) // 60


def _sessions(rows, min_bars: int) -> dict[int, dict]:
    """rows: (ts, open, high, low, close[, volume]) ascending -> complete
    sessions keyed by IST day."""
    days: dict[int, list] = {}
    for r in rows:
        days.setdefault(_day(r[0]), []).append(r)
    out = {}
    for d, bars in days.items():
        if len(bars) < min_bars:
            continue
        if _minute(bars[0][0]) > FIRST_BAR_BY or _minute(bars[-1][0]) < LAST_BAR_FROM:
            continue
        vol = [b[5] for b in bars if len(b) > 5 and b[5] is not None]
        out[d] = {"open": bars[0][1], "high": max(b[2] for b in bars),
                  "low": min(b[3] for b in bars), "close": bars[-1][4],
                  "volume": sum(vol) if vol else None}
    return out


def _extend(db: sqlite3.Connection, symbol: str, sessions: dict[int, dict],
            source: str) -> tuple[int, str | None]:
    row = db.execute("SELECT MAX(bar_ts) FROM candles WHERE symbol=? AND tf='day'",
                     (symbol,)).fetchone()
    last_day = _day(row[0]) if row and row[0] is not None else -1
    added, last_date = 0, None
    for d in sorted(sessions):
        if d <= last_day:
            continue
        s = sessions[d]
        bar_ts = _day_start(d)
        cur = db.execute(
            "INSERT OR IGNORE INTO candles(symbol, tf, bar_ts, open, high, low, close, volume) "
            "VALUES (?, 'day', ?, ?, ?, ?, ?, ?)",
            (symbol, bar_ts, s["open"], s["high"], s["low"], s["close"], s["volume"]))
        if cur.rowcount:
            db.execute("INSERT OR REPLACE INTO derived_daily(symbol, bar_ts, source, written_at) "
                       "VALUES (?, ?, ?, ?)", (symbol, bar_ts, source, int(time.time())))
            added += 1
            last_date = datetime.fromtimestamp(bar_ts + 43200, IST).strftime("%Y-%m-%d")
    return added, last_date


def main() -> int:
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS derived_daily (symbol TEXT NOT NULL, "
               "bar_ts INTEGER NOT NULL, source TEXT NOT NULL, written_at INTEGER, "
               "PRIMARY KEY (symbol, bar_ts))")
    for live, daily in LIVE_TO_DAILY.items():
        rows = db.execute(
            "SELECT bar_ts, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND tf='3m' ORDER BY bar_ts", (live,)).fetchall()
        added, last = _extend(db, daily, _sessions(rows, 120), f"live_3m:{live}")
        print(f"{daily}: +{added} daily bars from the live 3m feed"
              + (f" (through {last})" if last else ""))
    try:
        vdb = sqlite3.connect(f"file:{VIX_DB}?mode=ro", uri=True)
        rows = vdb.execute("SELECT ts, open, high, low, close FROM vix ORDER BY ts").fetchall()
        vdb.close()
    except sqlite3.Error as e:
        print(f"INDIAVIX: closing-lab store unreadable ({e}); skipped")
        rows = []
    if rows:
        added, last = _extend(db, "INDIAVIX", _sessions(rows, 60), "closing_vix_5m")
        print(f"INDIAVIX: +{added} daily bars from the closing lab store"
              + (f" (through {last})" if last else ""))
    db.commit()
    row = db.execute("SELECT bar_ts, close FROM candles WHERE symbol='INDIAVIX' AND tf='day' "
                     "ORDER BY bar_ts DESC LIMIT 1").fetchone()
    if row:
        print(f"INDIAVIX daily now ends {datetime.fromtimestamp(row[0] + 43200, IST):%Y-%m-%d}"
              f" at {row[1]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
