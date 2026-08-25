#!/usr/bin/env python3
"""Nightly gold-lab pass: refresh both venues' candles, recompute climatology.

Runs from evening_report.sh after the NIFTY pipeline. Fail-soft everywhere —
a stale Kite token or an unreachable Dukascopy evening must never break the
main report. Output: data/gold_climatology.json, rendered by
research_report.py as the "Gold lab" section.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
DB = DATA / "mcx_history.db"
OUT = DATA / "gold_climatology.json"
IST = timezone(timedelta(hours=5, minutes=30))


def refresh() -> list[str]:
    notes = []
    for script, args in (("backfill_mcx.py", []),
                         ("backfill_xauusd.py", ["--days", "7"])):
        try:
            r = subprocess.run([sys.executable, str(HERE / script), *args],
                               capture_output=True, text=True, timeout=600)
            last = (r.stdout.strip().splitlines() or ["no output"])[-1]
            notes.append(f"{script}: {last.split('  ', 1)[-1]}")
        except Exception as e:
            notes.append(f"{script}: refresh failed ({e}) — using stored data")
    return notes


def hour_shares(db: sqlite3.Connection, symbol: str, tf: str,
                tz_off_s: int, days: int) -> dict:
    cutoff = int(datetime.now(timezone.utc).timestamp()) - days * 86400
    rows = db.execute(
        "SELECT bar_ts, ABS(close - open) FROM candles "
        "WHERE symbol=? AND tf=? AND bar_ts>=?", (symbol, tf, cutoff)).fetchall()
    by_hr: dict[int, float] = {}
    for ts, mv in rows:
        h = ((ts + tz_off_s) % 86400) // 3600
        by_hr[h] = by_hr.get(h, 0.0) + (mv or 0.0)
    total = sum(by_hr.values()) or 1.0
    return {f"{h:02d}": round(100 * v / total, 1)
            for h, v in sorted(by_hr.items())}


def day_stats(db: sqlite3.Connection, symbol: str, n: int) -> dict:
    rows = db.execute(
        "SELECT open, high, low, close FROM candles "
        "WHERE symbol=? AND tf='day' ORDER BY bar_ts DESC LIMIT ?",
        (symbol, n)).fetchall()
    if len(rows) < 20:
        return {}
    rng = [100 * (h - lo) / c for _, h, lo, c in rows if c]
    gaps = [abs(100 * (rows[i][0] - rows[i + 1][3]) / rows[i + 1][3])
            for i in range(len(rows) - 1) if rows[i + 1][3]]
    return {
        "sessions": len(rows),
        "avg_range_pct": round(sum(rng) / len(rng), 2),
        "avg_gap_pct": round(sum(gaps) / len(gaps), 2),
        "gap_over_half_pct_share": round(
            100 * sum(1 for g in gaps if g > 0.5) / len(gaps)),
    }


def main() -> int:
    notes = refresh()
    db = sqlite3.connect(DB)
    xau_days = db.execute(
        "SELECT COUNT(DISTINCT bar_ts/86400) FROM candles "
        "WHERE symbol='XAUUSD' AND tf='1m'").fetchone()[0]
    out = {
        "updated": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "refresh_notes": notes,
        "mcx_hour_shares_ist": hour_shares(db, "GOLD_FUT", "3m", 19800, 150),
        "xau_hour_shares_ist": hour_shares(db, "XAUUSD", "1m", 19800, 150),
        "mcx_day": day_stats(db, "GOLD_FUT", 250),
        "xau_sessions_stored": xau_days,
    }
    OUT.write_text(json.dumps(out, indent=1))
    # Deploy the live page from its canonical, version-controlled copy —
    # same pattern as research.html (frontend/public/ is git-excluded).
    try:
        page_src = HERE / "gold_page.html"
        page_dst = HERE.parents[1] / "frontend" / "public" / "gold.html"
        page_dst.write_text(page_src.read_text())
    except Exception as e:
        print(f"gold page deploy skipped: {e}")
    print(f"gold climatology → {OUT.name} "
          f"(XAUUSD days stored: {xau_days})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
