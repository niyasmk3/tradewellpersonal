#!/usr/bin/env python3
"""Build app/closing/data/oi_prevclose.json.gz — the OI-wall shadow signal's
backfill — from a directory of NSE F&O bhavcopies.

    python scripts/build_oi_backfill.py /path/to/bhav_dir [--out PATH]

The directory holds one file per session, named YYYYMMDD.csv (UDiFF layout,
from https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip)
or YYYYMMDD.legacy.csv (pre-Jul-2024 layout, from
.../content/historical/DERIVATIVES/YYYY/MON/foDDMONYYYYbhav.csv.zip). Rows may
be pre-filtered to NIFTY. Each session's file is that session's CLOSE OI; the
card for the NEXT session reads it as "previous close".

Kept per session: NIFTY index options, expiries within EXPIRY_DAYS of the
session, strikes within +/-STRIKE_BAND of the session's close (spot in UDiFF,
near-month future close in legacy) — wide enough for the +/-500 wall span
around any 15:00 print. Output ~0.5MB gzipped for 3 years.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

EXPIRY_DAYS = 35
STRIKE_BAND = 1000.0


def udiff(path: Path):
    session = None; spot = None; opts = {}
    with open(path) as fh:
        for r in csv.reader(fh):
            if len(r) < 24 or r[7] != "NIFTY":
                continue
            session = session or r[0]
            if r[4] == "IDF" and spot is None:
                try: spot = float(r[20])
                except ValueError: pass
            elif r[4] == "IDO":
                try: opts.setdefault(r[9], {})[(float(r[11]), r[12])] = float(r[22])
                except ValueError: continue
    return session, spot, opts


def legacy(path: Path):
    session = None; fut = {}; opts = {}
    with open(path) as fh:
        for r in csv.reader(fh):
            if len(r) < 15 or r[1] != "NIFTY":
                continue
            try:
                exp = datetime.strptime(r[2], "%d-%b-%Y").date().isoformat()
                session = session or datetime.strptime(r[14], "%d-%b-%Y").date().isoformat()
            except ValueError:
                continue
            if r[0] == "FUTIDX":
                fut[exp] = float(r[8])
            elif r[0] == "OPTIDX":
                opts.setdefault(exp, {})[(float(r[3]), r[4])] = float(r[12])
    near = min((e for e in fut if e >= (session or "")), default=None)
    return session, (fut[near] if near else None), opts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bhav_dir")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1]
                                          / "backend" / "app" / "closing" / "data" / "oi_prevclose.json.gz"))
    a = ap.parse_args()
    out = {}
    files = sorted(Path(a.bhav_dir).glob("*.csv"))
    for f in files:
        session, centre, opts = (legacy(f) if f.name.endswith(".legacy.csv") else udiff(f))
        if not session or centre is None or not opts:
            print(f"skip {f.name}: session={session} centre={centre} expiries={len(opts)}", file=sys.stderr)
            continue
        horizon = (date.fromisoformat(session) + timedelta(days=EXPIRY_DAYS)).isoformat()
        keep = {}
        for exp, chain in opts.items():
            if not (session <= exp <= horizon):
                continue
            rows = [[s, k, oi] for (s, k), oi in chain.items() if abs(s - centre) <= STRIKE_BAND and oi > 0]
            if rows:
                keep[exp] = rows
        if keep:
            out[session] = keep
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(a.out, "wt") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"{len(out)} sessions -> {a.out} ({os.path.getsize(a.out)/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
