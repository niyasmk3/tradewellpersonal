#!/usr/bin/env python3
"""Parse a Kite Console F&O P&L statement (.xlsx) into the live book.

Console's P&L export is settled-data based, so it lags the current day by
one session — the open-position rows are valued at the PREVIOUS close, which
is how you can tell what it does and does not yet contain.

Reads the sheet with stdlib only (no openpyxl in the app venv, deliberately —
this must never require touching backend dependencies).

  backend/.venv/bin/python recorder/pnl_import.py ~/Downloads/pnl-*.xlsx
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DATA = Path(__file__).resolve().parent / "data"
OUT = DATA / "pnl_statement.json"


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"([A-Z]+)", ref or "A").group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def rows_of(path: Path):
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        shared = ["".join(t.text or "" for t in si.iter(NS + "t"))
                  for si in ET.fromstring(z.read("xl/sharedStrings.xml"))]
    sheet = sorted(n for n in z.namelist()
                   if n.startswith("xl/worksheets/sheet"))[0]
    for row in ET.fromstring(z.read(sheet)).iter(NS + "row"):
        vals: dict[int, str] = {}
        for c in row.iter(NS + "c"):
            v = c.find(NS + "v")
            if v is None or v.text is None:
                continue
            vals[_col(c.get("r"))] = (shared[int(v.text)]
                                      if c.get("t") == "s" else v.text)
        if vals:
            yield [vals.get(i, "") for i in range(max(vals) + 1)]


def num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main(paths: list[str]) -> int:
    path = Path(paths[0])
    summary: dict[str, float] = {}
    trades: list[dict] = []
    open_pos: list[dict] = []
    cols: dict[str, int] = {}
    for r in rows_of(path):
        head = (r[1] if len(r) > 1 else "").strip()
        if head in ("Charges", "Realized P&L", "Unrealized P&L",
                    "Other Credit & Debit") and len(r) > 2 and r[2]:
            summary.setdefault(head, num(r[2]))
        if head == "Symbol":
            # Map by header NAME — Console's column order has moved before
            # (the ISIN column sits between Symbol and Quantity).
            cols = {c.strip(): i for i, c in enumerate(r) if c.strip()}
            continue
        if cols and head.startswith("NIFTY"):
            def g(name, default=""):
                i = cols.get(name)
                return r[i] if i is not None and i < len(r) else default
            qty, realized = num(g("Quantity")), num(g("Realized P&L"))
            if qty:
                trades.append({"symbol": head, "qty": int(qty),
                               "buy_value": num(g("Buy Value")),
                               "sell_value": num(g("Sell Value")),
                               "realized": realized,
                               "pct": num(g("Realized P&L Pct."))})
            if num(g("Open Quantity")):
                open_pos.append({"symbol": head,
                                 "qty": int(num(g("Open Quantity"))),
                                 "side": g("Open Quantity Type"),
                                 "value": num(g("Open Value")),
                                 "unrealized": num(g("Unrealized P&L")),
                                 "prev_close": num(g("Previous Closing Price"))})

    wins = [t for t in trades if t["realized"] > 0]
    losses = [t for t in trades if t["realized"] < 0]
    gw = sum(t["realized"] for t in wins)
    gl = -sum(t["realized"] for t in losses)
    charges = summary.get("Charges", 0.0) - summary.get("Other Credit & Debit", 0.0)
    book = {
        "source": path.name,
        "summary": summary,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else None,
        "gross_realized": round(gw - gl, 2),
        "profit_factor": round(gw / gl, 2) if gl else None,
        "avg_win": round(gw / len(wins)) if wins else None,
        "avg_loss": round(gl / len(losses)) if losses else None,
        "net_after_charges": round(gw - gl - charges, 2),
        "open_positions": open_pos,
        "by_direction": {
            d: {"trades": sum(1 for t in trades if t["symbol"].endswith(d)),
                "wins": sum(1 for t in trades
                            if t["symbol"].endswith(d) and t["realized"] > 0),
                "net": round(sum(t["realized"] for t in trades
                                 if t["symbol"].endswith(d)))}
            for d in ("PE", "CE")},
        "rows": sorted(trades, key=lambda t: t["realized"]),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(book, indent=1))
    print(f"{book['trades']} round trips | {book['wins']}W/{book['losses']}L "
          f"= {book['win_rate_pct']}% | gross Rs{book['gross_realized']:+,.2f} "
          f"| PF {book['profit_factor']} | net Rs{book['net_after_charges']:+,.2f}")
    print(f"  PE {book['by_direction']['PE']} | CE {book['by_direction']['CE']}")
    for o in open_pos:
        print(f"  OPEN {o['symbol']} x{o['qty']} value Rs{o['value']:,.0f} "
              f"unreal Rs{o['unrealized']:+,.0f} (valued at prev close {o['prev_close']})")
    print("  worst:", ", ".join(f"{t['symbol']} {t['realized']:+,.0f}"
                                for t in book["rows"][:3]))
    print("  best :", ", ".join(f"{t['symbol']} {t['realized']:+,.0f}"
                                for t in book["rows"][-3:]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
