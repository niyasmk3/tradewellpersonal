"""Pre-registered filters for the Overnight tab — flags, never gates.

Round 4 of the study (docs/closing-day-strategy-2026-08-19.md) tested 46
filters over the confirmed trades and adversarially verified the top five.
Two came out worth registering:

  VOL EXPANSION (verified) — India VIX at 15:00 above its previous close OR
    above its own 09:15 open. Survived a 46-filter family-wise correction
    (joint FWER 0.027), sign-stable across three consecutive periods, helps
    both legs, and the edge shows up model-free in the index itself. The
    economic prior: a long ATM option is long vega; buy premium while vol is
    being bid and expansion pays part of the theta bill.

  MID-RANGE EXCLUSION (near-survivor) — the 15:00 print outside the middle
    0.35-0.65 of the day's range so far. Threshold-robust with a visible
    mechanism (the 0.4-0.6 deciles are toxic: a direction printed from
    mid-range is a direction the market has not committed to), but below
    family-wise significance on its own. Registered as the SECOND layer.

Why flags and not gates: the backtest holdout is SPENT — 46 filters were
mined against it, so these numbers are hypotheses, not results. The tab shows
each night's flags and a live scoreboard that grades ONLY nights after the
registration date; the headline strategy changes only if the live ledger
clears the house 30-sample rule. Registration date and definitions are frozen
here — editing them resets the live count.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date
from typing import Optional

import pandas as pd

from app.closing.calendar import ist_dt

REGISTERED_ON = date(2026, 8, 19)
MIN_LIVE_SAMPLE = 30   # house rule: no verdict below 30 filtered live nights

MID_LO, MID_HI = 0.35, 0.65

DEFINITIONS = [
    {
        "key": "vol_expand",
        "label": "Vol expansion",
        "rule": ("India VIX at 15:00 above its previous close OR above its own "
                 "09:15 open."),
        "status": "verified",
        "evidence": ("46-filter family-wise FWER 0.027; sign-stable in both "
                     "in-sample halves and the holdout; improves both CE and PE; "
                     "model-free in the index (+36.7 vs +18.5 signed pts). "
                     "Knife-edge caveat: readings within ~0.1 VIX pts of zero "
                     "flip on noise."),
    },
    {
        "key": "midrange",
        "label": "Mid-range exclusion",
        "rule": (f"The 15:00 print outside the middle {MID_LO}-{MID_HI} of the "
                 "day's range so far (range measured over bars up to 15:00)."),
        "status": "near-survivor",
        "evidence": ("Threshold-robust (every band 0.25-0.75 to 0.45-0.55 "
                     "improves both windows); the 0.4-0.6 deciles run -13%/-27% "
                     "mean; benefit spread across 24/33 months — but below "
                     "family-wise significance alone. Second layer only."),
    },
]


def _day_range_ctx(spine: pd.DataFrame) -> dict:
    """date -> (high, low) over bars up to and including the 15:00 bar —
    the same window the study's own features use."""
    ctx: dict = {}
    acc: dict = defaultdict(lambda: [float("-inf"), float("inf")])
    for ts, h, l in zip(spine["ts"].astype(int), spine["high"], spine["low"]):
        # IST-aware, never host-local: on a UTC box a naive fromtimestamp put
        # the 15:00 cutoff at 09:30 local and silently corrupted both flags
        # (review catch) — the exact drift the frozen registration forbids.
        dt = ist_dt(int(ts))
        if (dt.hour, dt.minute) > (15, 0):
            continue
        cur = acc[dt.date()]
        cur[0] = max(cur[0], float(h))
        cur[1] = min(cur[1], float(l))
    for d, (hi, lo) in acc.items():
        ctx[d] = (hi, lo)
    return ctx


def _vix_ctx(vix: pd.DataFrame) -> dict:
    """date -> {v0915, v_prev_close}. Prev close is the prior session's last
    stored bar, assigned strictly before the day it serves — no look-ahead."""
    per_day: dict = defaultdict(dict)
    for ts, c in zip(vix["ts"].astype(int), vix["close"].astype(float)):
        dt = ist_dt(int(ts))  # IST-aware — see _day_range_ctx
        per_day[dt.date()][(dt.hour, dt.minute)] = c
    out, prev_close = {}, None
    for d in sorted(per_day):
        bars = per_day[d]
        out[d] = {"v0915": bars.get((9, 15)), "v_prev_close": prev_close}
        prev_close = bars[max(bars)]
    return out


def annotate(trades: list, spine: pd.DataFrame, vix: pd.DataFrame) -> None:
    """Stamp each trade with the two flags plus the numbers behind them.

    Flags are True / False / None — None means the input was unavailable
    (missing VIX bar, zero range) and the night is UNKNOWN, never silently
    passed or failed.
    """
    rng = _day_range_ctx(spine)
    vc = _vix_ctx(vix)
    for t in trades:
        d = date.fromisoformat(t["date"])
        v = vc.get(d, {})
        v_in = t.get("vix_in")
        vs_prev = (round(v_in - v["v_prev_close"], 2)
                   if v_in is not None and v.get("v_prev_close") else None)
        day_chg = (round(v_in - v["v0915"], 2)
                   if v_in is not None and v.get("v0915") else None)
        t["vix_vs_prev"] = vs_prev
        t["vix_day_chg"] = day_chg
        # True if any KNOWN leg is positive; False only when BOTH legs are
        # known and non-positive. A night with one missing leg and no positive
        # reading is UNKNOWN — the missing leg may genuinely have fired, and
        # booking it as a definite fail would corrupt the live ledger
        # (review catch).
        legs = [x for x in (vs_prev, day_chg) if x is not None]
        if any(x > 0 for x in legs):
            t["f_vol_expand"] = True
        elif len(legs) == 2:
            t["f_vol_expand"] = False
        else:
            t["f_vol_expand"] = None

        hi_lo = rng.get(d)
        if hi_lo and hi_lo[0] > hi_lo[1]:
            pos = (t["signal_price"] - hi_lo[1]) / (hi_lo[0] - hi_lo[1])
            t["range_pos"] = round(pos, 3)
            t["f_midrange"] = not (MID_LO < pos < MID_HI)
        else:
            t["range_pos"] = None
            t["f_midrange"] = None

        t["f_all_pass"] = (bool(t["f_vol_expand"]) and bool(t["f_midrange"])
                           if t["f_vol_expand"] is not None
                           and t["f_midrange"] is not None else None)


def _compact(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    pct = [t["net_pct"] for t in trades]
    rs = [t["net_rs"] for t in trades]
    months = defaultdict(float)
    for t in trades:
        months[t["month"]] += t["net_rs"]
    peak = worst = cum = 0.0
    for x in rs:
        cum += x
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return {
        "n": len(trades),
        "win_pct": round(sum(1 for p in pct if p > 0) / len(pct) * 100, 1),
        "mean_pct": round(statistics.mean(pct), 2),
        "median_pct": round(statistics.median(pct), 2),
        "total_rs": round(sum(rs), 0),
        "max_dd_rs": round(worst, 0),
        "positive_months": sum(1 for x in months.values() if x > 0),
        "months": len(months),
    }


def ladder(trades: list) -> list:
    """base -> +vol_expand -> +both, each rung with what the step removed.

    Kept requires the flag to be strictly True; None (unknown) nights fall out
    at the rung that needed the missing input and are counted as removed with
    their own tally, so unknowns can never inflate a rung.
    """
    rungs = []
    base = list(trades)
    rungs.append({"label": "confirmed only (the strategy)", "kept": _compact(base)})
    vol = [t for t in base if t.get("f_vol_expand") is True]
    rungs.append({
        "label": "+ vol expansion",
        "kept": _compact(vol),
        "removed": _compact([t for t in base if t.get("f_vol_expand") is not True]),
        "unknown_n": sum(1 for t in base if t.get("f_vol_expand") is None),
    })
    both = [t for t in vol if t.get("f_midrange") is True]
    rungs.append({
        "label": "+ mid-range exclusion",
        "kept": _compact(both),
        "removed": _compact([t for t in vol if t.get("f_midrange") is not True]),
        "unknown_n": sum(1 for t in vol if t.get("f_midrange") is None),
    })
    return rungs


def live_scoreboard(trades: list) -> dict:
    """Nights strictly after the registration date — the only honest test the
    Round-4 numbers have left, since the backtest holdout was mined 46 times."""
    live = [t for t in trades if date.fromisoformat(t["date"]) > REGISTERED_ON]
    filtered = [t for t in live if t.get("f_all_pass") is True]
    return {
        "registered_on": REGISTERED_ON.isoformat(),
        "min_sample": MIN_LIVE_SAMPLE,
        "live_nights": len(live),
        "filtered_nights": len(filtered),
        "verdict_due": max(0, MIN_LIVE_SAMPLE - len(filtered)),
        "all": _compact(live),
        "filters_pass": _compact(filtered),
        "filters_fail": _compact([t for t in live if t.get("f_all_pass") is False]),
        "note": (f"Graded at {MIN_LIVE_SAMPLE}+ filtered live nights (house "
                 "rule). Until then this panel is a tally, not evidence."),
    }
