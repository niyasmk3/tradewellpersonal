#!/usr/bin/env python3
"""Import Kite Console tradebook CSVs into the true live book.

The app's journal is card-based bookkeeping the user maintains by hand; the
broker's tradebook is what ACTUALLY happened. This tool makes the tradebook
the system of record: FIFO-pairs buys and sells per contract into round
trips, dedupes by trade_id (re-importing the same or overlapping CSVs is
safe), and writes data/live_book.json — the ledger the paper-vs-real
slippage study reads.

  backend/.venv/bin/python recorder/tradebook_import.py ~/Downloads/tradebook-*.csv
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict, deque
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
BOOK = DATA / "live_book.json"
FILLS = DATA / "tradebook_fills.json"      # raw fills keyed by trade_id


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def main(paths: list[str]) -> int:
    fills = load(FILLS)
    new = 0
    for p in paths:
        with open(p, newline="") as fh:
            for r in csv.DictReader(fh):
                tid = f"{r['trade_date']}:{r['trade_id']}"
                if tid in fills:
                    continue
                fills[tid] = {
                    "symbol": r["symbol"], "type": r["trade_type"],
                    "qty": int(float(r["quantity"])),
                    "price": float(r["price"]),
                    "at": r["order_execution_time"],
                    "expiry": r.get("expiry_date") or "",
                }
                new += 1
    FILLS.parent.mkdir(parents=True, exist_ok=True)
    FILLS.write_text(json.dumps(fills, indent=1))

    # FIFO pairing per contract, unit-by-unit so partial/double lots pair
    # correctly (the 13-Aug 2-lot trade is why this is per-unit).
    by_sym: dict[str, list] = defaultdict(list)
    for f in fills.values():
        by_sym[f["symbol"]].append(f)
    trips, open_pos = [], []
    for sym, rows in by_sym.items():
        rows.sort(key=lambda f: f["at"])
        longs: deque = deque()
        for f in rows:
            if f["type"] == "buy":
                longs.append([f["qty"], f])
            else:
                q = f["qty"]
                while q > 0 and longs:
                    bq, bf = longs[0]
                    take = min(q, bq)
                    trips.append({
                        "symbol": sym, "qty": take,
                        "buy_at": bf["at"], "buy": bf["price"],
                        "sell_at": f["at"], "sell": f["price"],
                        "gross": round((f["price"] - bf["price"]) * take, 2),
                        "expiry": bf["expiry"],
                    })
                    q -= take
                    if take == bq:
                        longs.popleft()
                    else:
                        longs[0][0] -= take
        for bq, bf in longs:
            open_pos.append({"symbol": sym, "qty": bq, "buy": bf["price"],
                             "buy_at": bf["at"], "expiry": bf["expiry"]})

    trips.sort(key=lambda t: t["sell_at"])
    gross = round(sum(t["gross"] for t in trips), 2)
    wins = sum(1 for t in trips if t["gross"] > 0)
    BOOK.write_text(json.dumps({
        "source": "kite console tradebook (system of record)",
        "round_trips": trips, "open_positions": open_pos,
        "summary": {"trips": len(trips), "wins": wins,
                    "gross_rupees": gross,
                    "note": "gross of charges; ~Rs90/round-trip approx"},
    }, indent=1))
    print(f"{new} new fills | {len(trips)} round trips ({wins}W) | "
          f"gross ₹{gross:+,.2f} | open positions: {len(open_pos)}")
    for t in trips:
        print(f"  {t['sell_at'][:10]}  {t['symbol']:22s} x{t['qty']:3d} "
              f"{t['buy']:8.2f} → {t['sell']:8.2f}  ₹{t['gross']:+9,.2f}")
    for o in open_pos:
        print(f"  OPEN: {o['symbol']} x{o['qty']} @ {o['buy']} since {o['buy_at'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or [str(DATA / "tradebooks" / "latest.csv")]))
