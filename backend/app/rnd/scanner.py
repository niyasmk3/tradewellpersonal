"""R3 insight scanner — deterministic candidate discovery, nothing more.

A fixed rule sweep over the R2 conditioning dimensions that surfaces a cell
as a CANDIDATE hypothesis only when it clears, simultaneously:
  * n >= MIN_N (30, the house sample rule),
  * net premium-expectancy gap vs its complement >= GAP_PP,
  * the gap's SIGN holds on both sides of a FROZEN DEV/TEST date split.

Everything it emits is explicitly "candidate — pre-register its own ledger
before anything acts on it". The scanner never touches config, never feeds a
gate, and never ranks by anything it wasn't frozen to compute — MODEL D (the
audit's stacked-filter noise demonstration) is the cautionary tale this
design exists to not repeat.

Frozen registration (12-Aug-2026): DEV < 2026-08-04 00:00 IST <= TEST;
MIN_N = 30; GAP_PP = 0.5 (percentage points of premium move per trade).
Changing any of these resets every candidate's standing.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from app.rnd import analytics as A

_IST = timezone(timedelta(hours=5, minutes=30))

DEV_TEST_SPLIT = int(datetime(2026, 8, 4, tzinfo=_IST).timestamp())
MIN_N = 30
GAP_PP = 0.5

# The R2 conditioning dimensions, and only those. "mode" is deliberately NOT
# a dimension: modes differ by exit-policy construction (windows, stops,
# validity), so a mode-vs-rest gap measures the policies, not a condition
# (review C9).
DIMS = {
    "score_band": A._score_band,
    "tape_state": lambda t: t.get("tape_state"),
    "golden": lambda t: str(bool(t.get("golden"))),
    "entry_hour": A._hour,
    "day_of_week": A._dow,
    "direction": lambda t: t.get("direction"),
}


def _pnl_pct(t: dict) -> float | None:
    """Realized premium move per trade as % of entry outlay (gross — both
    sides of every comparison carry the same charges structure)."""
    entry, qty = t.get("entry_premium"), t.get("initial_quantity") or t.get("quantity")
    pnl = t.get("realized_pnl")
    if not entry or not qty or pnl is None:
        return None
    return pnl / (entry * qty) * 100.0


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def scan(cfg) -> dict:
    fills, _, _ = A._measurable(A.clean_fills(), cfg)
    rows = [(t, _pnl_pct(t)) for t in fills]
    rows = [(t, p) for t, p in rows if p is not None]
    candidates = []
    checked = 0
    for dim, keyfn in DIMS.items():
        # Cell AND complement are both restricted to rows the dimension can
        # classify — an unclassifiable row in the complement compares the
        # cell against a population it was never conditioned on (review C8).
        keyed = [(keyfn(t), t, p) for t, p in rows]
        keyed = [(k, t, p) for k, t, p in keyed if k is not None]
        groups: dict = {}
        for k, t, p in keyed:
            groups.setdefault(k, []).append((t, p))
        for key, cell in groups.items():
            checked += 1
            rest = [(t, p) for k2, t, p in keyed if k2 != key]
            if len(cell) < MIN_N or not rest:
                continue
            gap = _mean([p for _, p in cell]) - _mean([p for _, p in rest])
            if abs(gap) < GAP_PP:
                continue
            splits = []
            for lo, hi in ((0, DEV_TEST_SPLIT), (DEV_TEST_SPLIT, 1 << 62)):
                c = [p for t, p in cell if lo <= (t.get("entered_at") or 0) < hi]
                r = [p for t, p in rest if lo <= (t.get("entered_at") or 0) < hi]
                if not c or not r:
                    splits = []
                    break
                splits.append(_mean(c) - _mean(r))
            if len(splits) != 2 or (splits[0] > 0) != (splits[1] > 0) \
                    or (splits[0] > 0) != (gap > 0):
                continue
            candidates.append({
                "dim": dim, "key": str(key), "n": len(cell),
                "gap_pp": round(gap, 2),
                "dev_gap_pp": round(splits[0], 2),
                "test_gap_pp": round(splits[1], 2),
                "direction": "favorable" if gap > 0 else "adverse",
            })
    candidates.sort(key=lambda c: -abs(c["gap_pp"]))
    return {
        "generated_at": int(time.time()),
        "frozen": {"dev_test_split_ist": "2026-08-04 00:00",
                   "min_n": MIN_N, "gap_pp": GAP_PP},
        "cells_checked": checked,
        "candidates": candidates,
        "note": ("Candidates are HYPOTHESES, not signals: each needs its own "
                 "pre-registered ledger before anything acts on it. An empty "
                 "list is a valid — and common — result."),
    }
