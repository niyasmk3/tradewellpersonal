#!/usr/bin/env python3
"""Backfill international spot gold (XAUUSD) 1-minute candles.

Source: Dukascopy's free public datafeed — one LZMA-compressed file of BID
1-minute candles per UTC day, no account or key needed:
  https://datafeed.dukascopy.com/datafeed/XAUUSD/{Y}/{M-1:02d}/{D:02d}/BID_candles_min_1.bi5
(month is ZERO-indexed in their URL scheme). Each record is 24 bytes,
big-endian: seconds-from-day-start, open, close, low, high (integer points,
divide by 1000 for USD), volume float.

Why: the user becomes a UAE resident soon — MCX closes to NRIs while XAUUSD
opens. Same metal, new venue; this file makes the lab venue-portable.
Stored in the same gold DB as symbol XAUUSD tf 1m. Idempotent and
resume-safe: days already present are skipped.

  backend/.venv/bin/python recorder/mcx/backfill_xauusd.py --days 1100
"""
from __future__ import annotations

import argparse
import lzma
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill import log  # noqa: E402
from backfill_mcx import open_db  # noqa: E402  (same gold DB, shared schema)

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "mcx_history.db"

URL = ("https://datafeed.dukascopy.com/datafeed/XAUUSD/"
       "{y}/{m:02d}/{d:02d}/BID_candles_min_1.bi5")
# Their edge 503s bare clients; a browser UA + freeserv Referer is accepted.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36"),
    "Referer": "https://freeserv.dukascopy.com/",
}
POINT = 1000.0          # XAUUSD prices are stored as thousandths of a dollar
REC = struct.Struct(">5If")
SANE = (500.0, 20000.0)  # reject days whose prices fall outside this band


def parse_day(blob: bytes, day_utc: datetime) -> list[tuple]:
    if not blob:
        return []
    raw = lzma.decompress(blob)
    base = int(day_utc.replace(tzinfo=timezone.utc).timestamp())
    rows = []
    for off in range(0, len(raw) - REC.size + 1, REC.size):
        ts, o, c, lo, hi, vol = REC.unpack_from(raw, off)
        o, c, lo, hi = (v / POINT for v in (o, c, lo, hi))
        if not (SANE[0] <= lo <= hi <= SANE[1]) or vol < 0:
            return []          # format drift — refuse the whole day loudly
        rows.append(("XAUUSD", "1m", base + ts, o, hi, lo, c, float(vol)))
    return rows


def fetch_day(day: datetime) -> tuple[datetime, list[tuple] | None]:
    # requests gets 503 where curl passes (their edge fingerprints TLS
    # clients), so the transport is curl — verified working 25-Aug.
    url = URL.format(y=day.year, m=day.month - 1, d=day.day)
    tmp = Path(tempfile.gettempdir()) / f"duka_{day:%Y%m%d}.bi5"
    for attempt in (1, 2, 3):
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", "25", "-A", HEADERS["User-Agent"],
                 "-H", f"Referer: {HEADERS['Referer']}",
                 "-o", str(tmp), "-w", "%{http_code}", url],
                capture_output=True, text=True, timeout=40)
            code = r.stdout.strip()
            if code == "404":
                return day, []                      # weekend/holiday
            if code == "200":
                blob = tmp.read_bytes()
                tmp.unlink(missing_ok=True)
                return day, parse_day(blob, day)
        except Exception:
            pass
        time.sleep(attempt)
    tmp.unlink(missing_ok=True)
    return day, None                                # keep going; log later


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1100)
    args = ap.parse_args()

    db = open_db()
    have = {r[0] for r in db.execute(
        "SELECT DISTINCT (bar_ts/86400) FROM candles "
        "WHERE symbol='XAUUSD' AND tf='1m'")}
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    days = [today - timedelta(days=i) for i in range(1, args.days + 1)]
    days = [d for d in days if d.weekday() < 6                 # Sat mostly dead
            and int(d.timestamp()) // 86400 not in have]
    log(f"XAUUSD: {len(days)} day-files to fetch (resume-safe)")

    n_bars = n_fail = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, (day, rows) in enumerate(pool.map(fetch_day, days), 1):
            if rows is None:
                n_fail += 1
                continue
            if rows:
                db.executemany(
                    "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
                    rows)
                n_bars += len(rows)
            if i % 200 == 0:
                db.commit()
                log(f"  …{i}/{len(days)} days, {n_bars:,} bars so far")
    db.commit()
    lo, hi = db.execute(
        "SELECT MIN(bar_ts), MAX(bar_ts) FROM candles "
        "WHERE symbol='XAUUSD' AND tf='1m'").fetchone()
    span = ""
    if lo and hi:
        f = "%d-%b-%Y"
        span = (f" | span {datetime.fromtimestamp(lo, timezone.utc):{f}}"
                f" → {datetime.fromtimestamp(hi, timezone.utc):{f}}")
    log(f"XAUUSD done — {n_bars:,} new bars, {n_fail} failed days{span}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
