#!/usr/bin/env python3
"""Seller shadows — what the umbrella SHOP would have earned on our tape.

Three zero-risk studies, all from data we already record, refreshed nightly:

  1. FADE-THE-CARDS: the exact other side of every real paper fill — a seller
     shorting the same option at our entry and covering at our exit. By
     construction it is the mirror of the buyer's gross P&L, so it answers
     one question honestly: was the pain we felt the seller's gain, after
     the seller's own costs?
  2. THETA WINDOWS (intraday): sell the ATM straddle (CE+PE at the at-the-
     money strike) at a window's start, buy it back at the end — measured
     from our own 60-second chain snapshots. Windows: 10:30→12:00 (the
     audit's toxic-for-buyers window) and 14:15→15:20 (post entry-cutoff).
  3. OVERNIGHT DECAY (EOD): ATM straddle close-to-next-close from the NSE
     bhavcopy — the seller's classic "rent collected while you sleep",
     direction risk included.

Caveats stamped on every number: no margin costs modelled, seller charges
approximated, and window studies assume fills at the snapshot tape. These
are evidence for a decision at 30+ samples — not a strategy.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "data"
DB = DATA / "tradewell_history.db"
OUT = DATA / "seller_shadows.json"
IST = timezone(timedelta(hours=5, minutes=30))
LOT = 65
SELLER_CHARGES_PER_TRADE = 110.0   # brokerage+STT(sell)+txn+GST, rough per round trip


def log(m):
    print(m, flush=True)


def fade_the_cards() -> dict:
    try:
        fills = [t for t in json.loads((ROOT / "backend" / ".paper_trades.json").read_text())
                 if t.get("exited_at")
                 and not str(t.get("notes") or "").startswith("hollow")]
    except Exception:
        fills = []
    gross = sum((t["entry_premium"] - (t.get("exit_premium") or t["entry_premium"]))
                * t.get("quantity", LOT) for t in fills)
    net = gross - SELLER_CHARGES_PER_TRADE * len(fills)
    wins = sum(1 for t in fills
               if (t["entry_premium"] - (t.get("exit_premium") or t["entry_premium"])) > 0)
    return {"fills": len(fills), "seller_wins": wins,
            "gross": round(gross), "net_est": round(net)}


def _chain_at(db, day: str, hhmm: str):
    """Chain payload nearest to day+hhmm IST (within 10 min)."""
    target = f"{day}T{hhmm}"
    row = db.execute(
        "SELECT payload, ts_ist FROM snapshots WHERE endpoint='chain' AND symbol='NIFTY' "
        "AND ts_ist>=? ORDER BY ts_ist LIMIT 1", (target,)).fetchone()
    if not row or row[1][:16] > f"{day}T{hhmm[:2]}:{int(hhmm[3:]) + 10:02d}"[:16]:
        return None
    try:
        return json.loads(zlib.decompress(row[0]))
    except Exception:
        return None


def _straddle(chain) -> tuple[float, float] | None:
    if not chain:
        return None
    atm = chain.get("atm_strike")
    r = next((r for r in chain.get("rows", []) if r.get("strike") == atm), None)
    if not r or not r.get("ce_ltp") or not r.get("pe_ltp"):
        return None
    return atm, r["ce_ltp"] + r["pe_ltp"]


def theta_window(db, start: str, end: str) -> dict:
    days = [d[0][:10] for d in db.execute(
        "SELECT DISTINCT substr(ts_ist,1,10) FROM snapshots WHERE endpoint='chain'")]
    rows, total = [], 0.0
    for day in sorted(days):
        a, b = _chain_at(db, day, start), _chain_at(db, day, end)
        sa, sb = _straddle(a), _straddle(b)
        if not sa or not sb or sa[0] != sb[0]:
            continue
        pnl = (sa[1] - sb[1]) * LOT          # seller: collect open, pay back close
        total += pnl
        rows.append({"day": day, "strike": sa[0], "sold_at": round(sa[1], 2),
                     "bought_back": round(sb[1], 2), "pnl": round(pnl)})
    n = len(rows)
    return {"window": f"{start}→{end}", "days": n,
            "wins": sum(1 for r in rows if r["pnl"] > 0),
            "gross": round(total),
            "net_est": round(total - SELLER_CHARGES_PER_TRADE * 2 * n),
            "rows": rows[-10:]}


def overnight_decay(db) -> dict:
    spots = dict(db.execute(
        "SELECT date(bar_ts,'unixepoch','+330 minutes'), close FROM candles "
        "WHERE symbol='NIFTY_SPOT' AND tf='day'"))
    days = sorted(d[0] for d in db.execute("SELECT DISTINCT day FROM nse_fo_eod"))
    rows, total = [], 0.0
    for d1, d2 in zip(days, days[1:]):
        spot = spots.get(d1)
        if not spot:
            continue
        strike = round(spot / 50) * 50
        def straddle(day):
            got = db.execute(
                "SELECT opt_type, close, expiry FROM nse_fo_eod WHERE day=? AND symbol='NIFTY' "
                "AND strike=? AND opt_type IN ('CE','PE') AND expiry=("
                " SELECT MIN(expiry) FROM nse_fo_eod WHERE day=? AND symbol='NIFTY' "
                " AND strike=? AND expiry>=? AND opt_type IN ('CE','PE'))",
                (day, strike, day, strike, day)).fetchall()
            ce = next((c for t, c, e in got if t == "CE"), None)
            pe = next((c for t, c, e in got if t == "PE"), None)
            exp = got[0][2] if got else None
            return (ce + pe, exp) if ce and pe else (None, exp)
        s1, e1 = straddle(d1)
        s2, e2 = straddle(d2)
        if s1 is None or s2 is None or e1 != e2 or e1 <= d2:
            continue
        pnl = (s1 - s2) * LOT
        total += pnl
        rows.append({"held": f"{d1}→{d2}", "strike": strike,
                     "sold_at": round(s1, 2), "bought_back": round(s2, 2),
                     "pnl": round(pnl)})
    n = len(rows)
    return {"nights": n, "wins": sum(1 for r in rows if r["pnl"] > 0),
            "gross": round(total),
            "net_est": round(total - SELLER_CHARGES_PER_TRADE * n),
            "rows": rows[-10:]}


def main():
    db = sqlite3.connect(DB)
    out = {
        "generated": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "caveats": ("No margin cost modelled; seller charges ≈₹110/round-trip; window "
                    "fills assume the snapshot tape. Evidence for a 30+ sample verdict, "
                    "not a strategy."),
        "fade_the_cards": fade_the_cards(),
        "theta_toxic_window": theta_window(db, "10:30", "12:00"),
        "theta_late_window": theta_window(db, "14:15", "15:20"),
        "overnight_decay": overnight_decay(db),
    }
    OUT.write_text(json.dumps(out, indent=2))
    f = out["fade_the_cards"]
    log(f"seller shadows: fade {f['fills']} fills → seller net ≈ ₹{f['net_est']:+,}")
    for k in ("theta_toxic_window", "theta_late_window"):
        w = out[k]
        log(f"  straddle {w['window']}: {w['days']} days, {w['wins']} wins, net ≈ ₹{w['net_est']:+,}")
    o = out["overnight_decay"]
    log(f"  overnight ATM straddle: {o['nights']} nights, {o['wins']} wins, net ≈ ₹{o['net_est']:+,}")


if __name__ == "__main__":
    main()
