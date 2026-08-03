"""Support/resistance discovery with frequency counts.

Method: swing pivots (fractal highs/lows — a bar whose high/low is the extreme
of +/-PIVOT_K bars, i.e. +/-30 minutes) are clustered into fixed price bins.
A level's strength is the number of DISTINCT DAYS it was touched — one choppy
afternoon poking the same shelf twelve times is one piece of evidence, not
twelve (dedupe-by-day keeps a single session from fabricating a "major
level").

Per touch we also record whether the level held (price failed to close beyond
it by BREAK_PCT within the following 2 hours) or broke, giving each level a
hold-rate alongside its touch frequency. Round-number gravity (50/100
multiples) is measured separately.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PIVOT_K = 6            # bars each side => 30 min per side on 5-min bars
BIN_POINTS = 25.0      # ~0.1% of NIFTY at 25k; one bin = one "level"
BREAK_PCT = 0.0015     # close beyond level by 0.15% within window => broke
BREAK_WINDOW = 24      # bars (2 hours) to decide hold vs break
ROUND_TOL_PCT = 0.0004 # within 0.04% of a round number counts as "at" it


def _pivots(df: pd.DataFrame) -> pd.DataFrame:
    """Rows: ts, price, kind ('high'/'low'), date."""
    h, l = df["high"], df["low"]
    w = 2 * PIVOT_K + 1
    is_ph = h.eq(h.rolling(w, center=True).max())
    is_pl = l.eq(l.rolling(w, center=True).min())
    dates = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date
    piv = []
    for kind, mask, series in (("high", is_ph, h), ("low", is_pl, l)):
        sel = df.index[mask.fillna(False)]
        for i in sel:
            piv.append({"idx": i, "ts": int(df.at[i, "ts"]), "price": float(series.at[i]),
                        "kind": kind, "date": str(dates.at[i])})
    return pd.DataFrame(piv).sort_values("ts").reset_index(drop=True) if piv else pd.DataFrame(
        columns=["idx", "ts", "price", "kind", "date"])


def _held_or_broke(df: pd.DataFrame, piv_row) -> str:
    """Did price close decisively beyond the pivot within BREAK_WINDOW bars?"""
    i = int(piv_row["idx"])
    price = piv_row["price"]
    fwd = df["close"].iloc[i + 1: i + 1 + BREAK_WINDOW]
    if fwd.empty:
        return "open"
    thr = price * BREAK_PCT
    if piv_row["kind"] == "high":
        return "broke" if (fwd > price + thr).any() else "held"
    return "broke" if (fwd < price - thr).any() else "held"


def find_levels(df: pd.DataFrame, top_n: int = 40) -> dict:
    piv = _pivots(df)
    if piv.empty:
        return {"levels": [], "round_number_stats": {}, "pivot_count": 0}

    piv["outcome"] = [_held_or_broke(df, r) for _, r in piv.iterrows()]
    piv["bin"] = (piv["price"] / BIN_POINTS).round().astype(int)

    rows = []
    for b, grp in piv.groupby("bin"):
        days = grp["date"].nunique()
        rows.append({
            "level": round(b * BIN_POINTS, 1),
            "days_touched": int(days),
            "total_touches": int(len(grp)),
            "as_resistance": int((grp["kind"] == "high").sum()),
            "as_support": int((grp["kind"] == "low").sum()),
            "held": int((grp["outcome"] == "held").sum()),
            "broke": int((grp["outcome"] == "broke").sum()),
            "hold_rate": round(float((grp["outcome"] == "held").sum() / max(1, (grp["outcome"] != "open").sum())), 3),
            "first_touch": grp["date"].min(),
            "last_touch": grp["date"].max(),
        })
    rows.sort(key=lambda r: (-r["days_touched"], -r["total_touches"]))

    # Round-number gravity: are pivots overrepresented near 50/100 multiples?
    # Expected share under uniformity = 2*tol_points/step for each step size.
    price_med = float(piv["price"].median())
    tol = price_med * ROUND_TOL_PCT
    rn = {}
    for step in (100, 50):
        near = ((piv["price"] % step).apply(lambda x, s=step: min(x, s - x)) <= tol)
        rn[f"near_{step}"] = {
            "pivots_at_round": int(near.sum()),
            "share": round(float(near.mean()), 4),
            "expected_share_if_random": round(2 * tol / step, 4),
        }

    return {
        "method": f"fractal pivots +/-{PIVOT_K * 5}min, {BIN_POINTS:.0f}-pt bins, "
                  f"break = close beyond {BREAK_PCT * 100:.2f}% within {BREAK_WINDOW * 5}min",
        "pivot_count": int(len(piv)),
        "levels": rows[:top_n],
        "round_number_stats": rn,
    }


def levels_near(results_levels: list, price: float, window_pct: float = 2.0) -> list:
    lo, hi = price * (1 - window_pct / 100), price * (1 + window_pct / 100)
    return [lv for lv in results_levels if lo <= lv["level"] <= hi]
