#!/usr/bin/env python3
"""Experiment bench: one-screen summary of everything Tradewell has learned.

Reads the app's own evidence files (signal archive, trade journal, paper book)
plus the sidecar recorder DB, and prints counts, date ranges and outcome
splits. Safe to run any time — missing files just report as "nothing yet".
Stdlib only. For deeper work, open JupyterLab:

    backend/.venv/bin/jupyter lab

and load the same files with pandas (see recorder/README.md for snippets).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DB = ROOT / "recorder" / "data" / "tradewell_history.db"


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def load_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def dig_trades(payload: object) -> list[dict]:
    """The store file may be a list or a dict wrapping a list — take either."""
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def summarize_cards(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"{label}: nothing yet")
        return
    # Archive semantics: last line per card id is its final state. Lines wrap
    # the card: {"archived_at": ..., "card": {...}}.
    final: dict[str, dict] = {}
    for r in rows:
        r = r.get("card") if isinstance(r.get("card"), dict) else r
        cid = str(r.get("id") or r.get("card_id") or len(final))
        final[cid] = r
    cards = list(final.values())
    print(f"{label}: {len(cards)} cards ({len(rows)} events)")
    for field in ("symbol", "mode", "direction", "status", "retire_reason"):
        vals = Counter(str(c.get(field)) for c in cards if c.get(field) is not None)
        if vals:
            top = ", ".join(f"{k}:{n}" for k, n in vals.most_common(6))
            print(f"  by {field:14s} {top}")


def summarize_trades(trades: list[dict], label: str) -> None:
    if not trades:
        print(f"{label}: nothing yet")
        return
    print(f"{label}: {len(trades)} rows")
    status = Counter(str(t.get("status")) for t in trades if t.get("status"))
    if status:
        print(f"  by status: {', '.join(f'{k}:{n}' for k, n in status.most_common())}")
    # Never count bookkeeping mistakes: dismissed rows, and rows a human has
    # flagged PHANTOM in notes (mis-typed fills the app faithfully graded —
    # the `ignore` endpoint only works on OPEN trades, so a closed mistake can
    # only be neutralised by the note).
    graded = [t for t in trades
              if str(t.get("status", "")).lower() != "ignored"
              and not str(t.get("notes") or "").upper().startswith("PHANTOM")]
    reasons = Counter(str(t.get("exit_reason")) for t in graded if t.get("exit_reason"))
    if reasons:
        print(f"  by exit: {', '.join(f'{k}:{n}' for k, n in reasons.most_common())}")
    pnls = [t.get("realized_pnl") or t.get("pnl") for t in graded]
    pnls = [p for p in pnls if isinstance(p, (int, float))]
    if pnls:
        wins = [p for p in pnls if p > 0]
        print(
            f"  closed with P&L (gross, pre-charges): {len(pnls)}  |  "
            f"win-rate {len(wins)}/{len(pnls)}  |  total {sum(pnls):,.0f}  |  "
            f"avg {sum(pnls)/len(pnls):,.0f}"
        )


def main() -> None:
    section("Signal archive (permanent record)")
    summarize_cards(load_jsonl(BACKEND / ".signals_archive.jsonl"), "signals")

    section("Hollow signals (vetoed but paper-tracked)")
    hollow = load_json(BACKEND / ".hollow_signals.json")
    summarize_cards(dig_trades(hollow) if hollow else [], "hollow")

    section("Trade journal (.trades.json — manual/live rows)")
    summarize_trades(dig_trades(load_json(BACKEND / ".trades.json")), "journal")

    section("Paper book (simulated fills, net of charges)")
    paper_files = sorted(BACKEND.glob(".paper*.json*"))
    if not paper_files:
        print("paper book: nothing yet (fills start on the first live session)")
    for pf in paper_files:
        summarize_trades(dig_trades(load_json(pf)), pf.name)

    section("Recorder DB (chain/OI + candles history)")
    if not DB.exists():
        print("db: not created yet (recorder writes it on first market session)")
        return
    db = sqlite3.connect(DB)
    snaps = db.execute(
        "SELECT endpoint, COUNT(*), MIN(ts_ist), MAX(ts_ist) FROM snapshots GROUP BY endpoint"
    ).fetchall()
    if snaps:
        for ep, n, lo, hi in snaps:
            print(f"  {ep:16s} {n:8,d} rows   {lo} → {hi}")
    else:
        print("  snapshots: none yet")
    bars = db.execute(
        "SELECT symbol, tf, COUNT(*) FROM candles GROUP BY symbol, tf"
    ).fetchall()
    for sym, tf, n in bars:
        print(f"  candles {sym:10s} {tf:4s} {n:8,d} bars")
    size_mb = DB.stat().st_size / 1e6
    print(f"  db size: {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
