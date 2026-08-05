#!/usr/bin/env python3
"""Nightly NSE archive collector — free EOD data the live feed can't give.

Two files per trading day, straight from nsearchives.nseindia.com (verified
open, no cookie wall, 05-Aug-2026):

  1. fao_participant_oi_DDMMYYYY.csv — FII / DII / Pro / Client long-short
     positions in index futures & options. "What the smart money holds",
     daily. → table nse_participant_oi (one row per participant class).
  2. BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip — EVERY F&O contract's
     OHLC + OI + volume. Filtered to our three underlyings' options, this is
     the EOD option-chain history Kite cannot provide for expired contracts.
     → table nse_fo_eod (one row per contract per day).

Idempotent: a fetched_days table tracks what's stored; each run catches up
the last 10 calendar days, so missed evenings self-heal. Weekends/holidays
404 and are skipped quietly.
"""
from __future__ import annotations

import csv
import io
import sqlite3
import sys
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "tradewell_history.db"
IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY"}
CATCHUP_DAYS = 10


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception:
        return None


def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS nse_participant_oi (
             day TEXT NOT NULL, participant TEXT NOT NULL,
             fut_idx_long REAL, fut_idx_short REAL,
             opt_idx_call_long REAL, opt_idx_put_long REAL,
             opt_idx_call_short REAL, opt_idx_put_short REAL,
             PRIMARY KEY (day, participant))"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS nse_fo_eod (
             day TEXT NOT NULL, symbol TEXT NOT NULL, instrument TEXT,
             expiry TEXT, strike REAL, opt_type TEXT,
             close REAL, settle REAL, oi REAL, oi_chg REAL, volume REAL,
             PRIMARY KEY (day, symbol, expiry, strike, opt_type, instrument))"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS nse_fetched_days (
             day TEXT NOT NULL, source TEXT NOT NULL,
             PRIMARY KEY (day, source))"""
    )
    return db


def have(db: sqlite3.Connection, day: str, source: str) -> bool:
    return db.execute(
        "SELECT 1 FROM nse_fetched_days WHERE day=? AND source=?", (day, source)
    ).fetchone() is not None


def mark(db: sqlite3.Connection, day: str, source: str) -> None:
    db.execute("INSERT OR IGNORE INTO nse_fetched_days VALUES (?,?)", (day, source))


def pull_participant_oi(db: sqlite3.Connection, d: datetime) -> bool:
    day = d.strftime("%Y-%m-%d")
    if have(db, day, "participant_oi"):
        return False
    raw = fetch(
        "https://nsearchives.nseindia.com/content/nsccl/"
        f"fao_participant_oi_{d.strftime('%d%m%Y')}.csv"
    )
    if raw is None:
        return False
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8", "ignore"))))
    # Header spans two lines; data rows start with the participant class name.
    n = 0
    for r in rows:
        if not r or r[0].strip() not in {"Client", "DII", "FII", "Pro", "TOTAL"}:
            continue
        try:
            vals = [float(x.replace(",", "")) for x in r[1:9]]
        except (ValueError, IndexError):
            continue
        # Columns: fut idx long/short, fut stk long/short, call idx long,
        # put idx long, call idx short, put idx short  (per NSE layout).
        db.execute(
            "INSERT OR REPLACE INTO nse_participant_oi VALUES (?,?,?,?,?,?,?,?)",
            (day, r[0].strip(), vals[0], vals[1], vals[4], vals[5], vals[6], vals[7]),
        )
        n += 1
    if n:
        mark(db, day, "participant_oi")
        log(f"  participant OI {day}: {n} rows")
    return n > 0


def pull_bhavcopy(db: sqlite3.Connection, d: datetime) -> bool:
    day = d.strftime("%Y-%m-%d")
    if have(db, day, "bhavcopy"):
        return False
    raw = fetch(
        "https://nsearchives.nseindia.com/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
    )
    if raw is None:
        return False
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        text = zf.read(zf.namelist()[0]).decode("utf-8", "ignore")
    except Exception:
        return False
    rdr = csv.DictReader(io.StringIO(text))
    n = 0
    for r in rdr:
        if r.get("TckrSymb") not in UNDERLYINGS:
            continue
        try:
            db.execute(
                "INSERT OR REPLACE INTO nse_fo_eod VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    day, r["TckrSymb"], r.get("FinInstrmTp"),
                    r.get("XpryDt"), float(r.get("StrkPric") or 0),
                    r.get("OptnTp") or "", float(r.get("ClsPric") or 0),
                    float(r.get("SttlmPric") or 0), float(r.get("OpnIntrst") or 0),
                    float(r.get("ChngInOpnIntrst") or 0),
                    float(r.get("TtlTradgVol") or 0),
                ),
            )
            n += 1
        except (ValueError, KeyError):
            continue
    if n:
        mark(db, day, "bhavcopy")
        log(f"  bhavcopy {day}: {n} contracts stored")
    return n > 0


def main() -> int:
    db = open_db()
    today = datetime.now(IST)
    got = 0
    for back in range(CATCHUP_DAYS):
        d = today - timedelta(days=back)
        if d.weekday() > 4:
            continue
        if pull_participant_oi(db, d):
            got += 1
        if pull_bhavcopy(db, d):
            got += 1
    db.commit()
    if got == 0:
        log("  nse archives: nothing new (all caught up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
