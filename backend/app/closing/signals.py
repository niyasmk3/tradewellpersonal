"""Which 15:00 rule picks the side best? A pre-registered candidate sweep.

LAYER 1 ONLY, deliberately. Every number here is the signed overnight move in
real index points — the option model never touches this file. Searching a
modelled P&L surface for its best rule would be shopping for the assumption
that flatters you most; the index either leans a way or it does not.

TWO CONTROLS MAKE THIS HONEST, and both are reported as first-class rows:

  ALWAYS CE / ALWAYS PE.  NIFTY drifts up. Over three years the unconditional
    overnight move is about +7 points, so a rule that happens to pick CE on 54%
    of nights collects part of that for free and looks skilful. Any candidate
    that cannot beat "always buy CE" has found nothing.

  SKILL (drift-adjusted mean).  mean - (CE_share - PE_share) x unconditional
    drift. This strips out what the candidate's net long/short exposure earns
    from the drift alone, leaving what it earned from being RIGHT. It is the
    column to rank on; the raw mean is the column that misleads.

Results are reported per window — an in-sample stretch and a holdout — because
a candidate that wins on the pooled sample and flips sign on the holdout is
noise, and there are several of those here. The candidate count is published
with every result: these p-values are UNCORRECTED for multiple comparisons.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Callable, Optional

from app.closing.study import (
    EXIT_BAR,
    OPEN_BAR,
    SIGNAL_BAR,
    Day,
    _bootstrap_ci,
    close_ref,
)

MIN_SAMPLE = 30   # house rule: below 30 a cell is an anecdote


def build_rows(days: "dict[date, Day]", start: Optional[date] = None) -> list:
    """Per-night feature rows + the realised overnight target.

    Everything in a row is knowable at 15:00; the fill is the 15:00 bar's
    close and the target runs from there to the next 09:50 print, so no
    feature can see its own outcome.
    """
    ordered = sorted(days)
    rows = []
    for i in range(2, len(ordered) - 1):
        d = ordered[i]
        day, prev_day, p2_day, next_day = (days[d], days[ordered[i - 1]],
                                           days[ordered[i - 2]], days[ordered[i + 1]])
        sig_bar, open_bar = day.bar(SIGNAL_BAR), day.bar(OPEN_BAR)
        exit_bar = next_day.bar(EXIT_BAR)
        pclose, pclose2 = close_ref(prev_day), close_ref(p2_day)
        if not (sig_bar and open_bar and exit_bar and pclose and pclose2):
            continue
        seen = [day.bars[k] for k in sorted(day.bars) if k <= SIGNAL_BAR]
        b14, b13 = day.bar((14, 0)), day.bar((13, 0))
        rows.append({
            "date": d,
            "p1500": sig_bar[0],
            "open": open_bar[0],
            "prev_close": pclose,
            "prev_close2": pclose2,
            "high": max(b[1] for b in seen),
            "low": min(b[2] for b in seen),
            "p1400": b14[0] if b14 else None,
            "p1300": b13[0] if b13 else None,
            "target": exit_bar[0] - sig_bar[3],
        })
    # Yesterday's realised overnight move, for the autocorrelation candidate.
    for i, r in enumerate(rows):
        r["prev_overnight"] = rows[i - 1]["target"] if i > 0 else None
    if start:
        rows = [r for r in rows if r["date"] >= start]
    return rows


def _body(r):
    return r["p1500"] - r["open"]


def _gap(r):
    return r["p1500"] - r["prev_close"]


def _sign(x) -> int:
    return 1 if x > 0 else -1


# (key, label, fn). fn returns +1 for CE, -1 for PE, or None to stand aside.
CANDIDATES: "list[tuple[str, str, Callable]]" = [
    ("day_open", "vs today's open", lambda r: _sign(_body(r))),
    ("prev_close", "vs previous close", lambda r: _sign(_gap(r))),
    ("two_day", "vs close two days back", lambda r: _sign(r["p1500"] - r["prev_close2"])),
    ("last_hour", "vs 14:00 (last hour)", lambda r: None if r["p1400"] is None
     else _sign(r["p1500"] - r["p1400"])),
    ("last_2h", "vs 13:00 (last two hours)", lambda r: None if r["p1300"] is None
     else _sign(r["p1500"] - r["p1300"])),
    ("range_pos", "upper half of day's range", lambda r: None if r["high"] == r["low"]
     else _sign((r["p1500"] - r["low"]) / (r["high"] - r["low"]) - 0.5)),
    ("morning_gap", "this morning's gap direction", lambda r: _sign(r["open"] - r["prev_close"])),
    ("autocorr", "yesterday's overnight move", lambda r: None if r["prev_overnight"] is None
     else _sign(r["prev_overnight"])),
    ("both_agree", "open AND prev close agree", lambda r: _sign(_body(r))
     if (_body(r) > 0) == (_gap(r) > 0) else None),
    ("quiet_body", "vs open, |body| < 50 pts", lambda r: None if abs(_body(r)) >= 50
     else _sign(_body(r))),
    # Momentum confirmation (added 19-Aug after the 14-Aug post-mortem: day
    # green, last hour falling, index fell overnight). Follow the body ONLY
    # when the last hour points the same way; a disagreement night is a
    # stand-aside, not a fade — "disagree -> follow the last hour" scored
    # NEGATIVE skill in all three windows when tested. Honesty note: this
    # candidate was motivated by inspecting a specific losing night, so its
    # holdout number is partially self-selected; it stays a candidate until
    # live nights accumulate.
    ("lasthr_confirm", "vs open, last hour agrees", lambda r: None
     if r["p1400"] is None or _body(r) == 0 or r["p1500"] == r["p1400"]
     or (_body(r) > 0) != (r["p1500"] - r["p1400"] > 0)
     else _sign(_body(r))),
    ("loud_body", "vs open, |body| > 100 pts", lambda r: None if abs(_body(r)) <= 100
     else _sign(_body(r))),
    ("fade_open", "FADE today's open", lambda r: -_sign(_body(r))),
    ("always_ce", "ALWAYS CE (control)", lambda r: 1),
    ("always_pe", "ALWAYS PE (control)", lambda r: -1),
]


def evaluate(rows: list, iters: int = 3000) -> dict:
    """Score every candidate on one window."""
    if len(rows) < MIN_SAMPLE:
        return {"n": len(rows), "results": [], "drift_pts": None}
    drift = statistics.mean(r["target"] for r in rows)
    out = []
    for key, label, fn in CANDIDATES:
        signed, longs = [], 0
        for r in rows:
            s = fn(r)
            if s is None:
                continue
            signed.append(r["target"] * s)
            longs += 1 if s > 0 else 0
        if len(signed) < MIN_SAMPLE:
            continue
        n = len(signed)
        ce_share = longs / n
        mean = statistics.mean(signed)
        ci = _bootstrap_ci(signed, iters)
        out.append({
            "key": key,
            "label": label,
            "n": n,
            "traded_pct": round(n / len(rows) * 100, 1),
            "ce_share_pct": round(ce_share * 100, 1),
            "hit_pct": round(sum(1 for x in signed if x > 0) / n * 100, 1),
            "mean_pts": round(mean, 2),
            "median_pts": round(statistics.median(signed), 2),
            "mean_ci95": ci,
            # What it earned from being right, not from being net long.
            "skill_pts": round(mean - (2 * ce_share - 1) * drift, 2),
            "clears_zero": bool(ci and (ci[0] > 0 or ci[1] < 0)),
        })
    out.sort(key=lambda x: -x["skill_pts"])
    return {"n": len(rows), "drift_pts": round(drift, 2),
            "drift_median_pts": round(statistics.median(r["target"] for r in rows), 2),
            "results": out}


def search(days: "dict[date, Day]", today: Optional[date] = None) -> dict:
    """The full sweep across a holdout split, plus the pooled window."""
    today = today or max(days) if days else None
    if today is None:
        return {"available": False, "reason": "no sessions"}
    three = today - timedelta(days=365 * 3)
    one = today - timedelta(days=365)
    all_rows = build_rows(days, start=three)
    windows = {
        "pooled_3y": evaluate(all_rows),
        "in_sample_first_2y": evaluate([r for r in all_rows if r["date"] < one]),
        "holdout_last_1y": evaluate([r for r in all_rows if r["date"] >= one]),
    }
    # A candidate is only interesting if it clears zero on the holdout it never
    # informed. Everything else is a story about the past.
    holdout = {r["key"]: r for r in windows["holdout_last_1y"]["results"]}
    survivors = [r["key"] for r in windows["pooled_3y"]["results"]
                 if r["clears_zero"] and holdout.get(r["key"], {}).get("clears_zero")
                 and not r["key"].startswith("always")]
    return {
        "available": True,
        "windows": windows,
        "candidates_tested": len(CANDIDATES),
        "survivors": survivors,
        "note": (
            f"{len(CANDIDATES)} candidates scored on 3 windows, UNCORRECTED for "
            "multiple comparisons. Rank on `skill`, not `mean`: skill removes "
            "what a candidate's net long/short exposure earns from NIFTY's "
            "upward drift, which is the whole of what the ALWAYS CE control "
            "collects. `survivors` are the candidates that cleared zero on both "
            "the pooled window and the holdout."
        ),
    }
