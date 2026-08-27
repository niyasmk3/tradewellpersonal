#!/usr/bin/env python3
"""NIFTY 3-year climatology — market habits as knowledge, not signals.

Computed from the deep-backfilled NIFTY_SPOT 3m candles (~745 sessions).
Pure context for the human and, later, priors for the September research
run: gap-fill base rates, opening-range continuation, movement-by-hour,
weekday ranges. No rules are graded here and none may be promoted from
here — a climatology is a map, not a trade.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
DB = DATA / "tradewell_history.db"
OUT = DATA / "nifty_climatology.json"
IST = timezone(timedelta(hours=5, minutes=30))


def main() -> int:
    db = sqlite3.connect(DB)
    rows = db.execute(
        "SELECT bar_ts, open, high, low, close FROM candles "
        "WHERE symbol='NIFTY_SPOT' AND tf='3m' ORDER BY bar_ts").fetchall()
    days: dict[int, list] = {}
    for r in rows:
        days.setdefault((r[0] + 19800) // 86400, []).append(r)
    keys = sorted(days)

    gaps_03, gaps_05 = [], []          # (gap_pct, filled)
    cont = []                          # opening-range continuation samples
    by_hr: dict[int, float] = {}
    by_dow: dict[int, list] = {}
    prev_close = None
    for d in keys:
        bars = days[d]
        if len(bars) < 20:
            prev_close = bars[-1][4] if bars else prev_close
            continue
        o = bars[0][1]
        hi = max(b[2] for b in bars)
        lo = min(b[3] for b in bars)
        c = bars[-1][4]
        dow = datetime.fromtimestamp(d * 86400 - 19800 + 43200, IST).weekday()
        by_dow.setdefault(dow, []).append(100 * (hi - lo) / c)
        for b in bars:
            h = ((b[0] + 19800) % 86400) // 3600
            by_hr[h] = by_hr.get(h, 0.0) + abs(b[4] - b[1])
        if prev_close:
            gap = 100 * (o - prev_close) / prev_close
            filled = (lo <= prev_close) if gap > 0 else (hi >= prev_close)
            if abs(gap) >= 0.3:
                gaps_03.append(filled)
            if abs(gap) >= 0.5:
                gaps_05.append(filled)
        # opening range 09:15-09:45 vs rest of day, both ≥ 0.15% to count
        orb = [b for b in bars if ((b[0] + 19800) % 86400) // 60 < 585]
        if orb:
            m1 = 100 * (orb[-1][4] - o) / o
            m2 = 100 * (c - orb[-1][4]) / orb[-1][4]
            if abs(m1) >= 0.15 and abs(m2) >= 0.05:
                cont.append(m1 * m2 > 0)
        prev_close = c

    total_mv = sum(by_hr.values()) or 1.0
    toxic = sum(v for h, v in by_hr.items() if 10 <= h < 12)
    dows = "Mon Tue Wed Thu Fri Sat Sun".split()   # Sat: Budget-day sessions
    out = {
        "updated": datetime.now(IST).strftime("%Y-%m-%d"),
        "sessions": len(keys),
        "gap_03": {"n": len(gaps_03),
                   "fill_pct": round(100 * sum(gaps_03) / len(gaps_03))
                   if gaps_03 else None},
        "gap_05": {"n": len(gaps_05),
                   "fill_pct": round(100 * sum(gaps_05) / len(gaps_05))
                   if gaps_05 else None},
        "orb_continuation": {"n": len(cont),
                             "pct": round(100 * sum(cont) / len(cont))
                             if cont else None},
        "toxic_window_share_pct": round(100 * toxic / total_mv, 1),
        "avg_range_by_dow": {dows[k]: round(sum(v) / len(v), 2)
                             for k, v in sorted(by_dow.items()) if v},
    }
    OUT.write_text(json.dumps(out, indent=1))
    print(f"nifty climatology → {OUT.name} ({len(keys)} sessions)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
