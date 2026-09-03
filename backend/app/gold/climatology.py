"""Climatology — the base rates computed BEFORE any rule is graded (spec §3).

These are the priors that stop small-sample lies later: hour-of-day travel
share, daily range, and the overnight gap distribution, computed independently
on both venues. The published cross-check is whether MCX and XAUUSD show the
SAME busiest hours from two unrelated data sources — the first run of this
study found they do (19, 18, 20 IST).

Everything here is measurement over stored candles: no thresholds, no rules,
no opinions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
_IST_OFFSET_S = 19_800

EVENING_HOURS = (17, 18, 19, 20)   # 17:00–21:00 IST, the US-driven window
GAP_THRESHOLD_PCT = 0.5


def _hourly_travel(ts: pd.Series, opens: pd.Series, closes: pd.Series) -> list:
    """Share of total |close-open| travel per IST hour, as the tab's bars."""
    hours = ((ts + _IST_OFFSET_S) // 3600) % 24
    travel = (closes - opens).abs()
    by_hour = travel.groupby(hours).sum()
    total = float(by_hour.sum())
    if total <= 0:
        return []
    return [{"hour": int(h), "share_pct": round(100.0 * float(v) / total, 1)}
            for h, v in by_hour.sort_index().items()]


def _busiest(hourly: list, n: int = 3) -> list:
    return [x["hour"] for x in sorted(hourly, key=lambda x: -x["share_pct"])[:n]]


def _evening_share(hourly: list) -> Optional[float]:
    if not hourly:
        return None
    return round(sum(x["share_pct"] for x in hourly if x["hour"] in EVENING_HOURS), 1)


def mcx_block(m3: pd.DataFrame, day: pd.DataFrame) -> Optional[dict]:
    """Intraday shape from the 3m spine; range/gap stats from the ~5y
    continuous day candles (the 3m window is too short to speak about gaps)."""
    if m3 is None or len(m3) == 0:
        return None
    hourly = _hourly_travel(m3["ts"], m3["open"], m3["close"])
    sessions = pd.to_datetime(m3["ts"] + _IST_OFFSET_S, unit="s").dt.date.nunique()
    out = {
        "sessions": int(sessions),
        "hourly_travel": hourly,
        "busiest_hours_ist": _busiest(hourly),
        "evening_share_pct": _evening_share(hourly),
    }
    if day is not None and len(day) > 1:
        prev_close = day["close"].shift(1)
        gap_pct = ((day["open"] - prev_close) / prev_close * 100.0).dropna()
        range_pct = ((day["high"] - day["low"]) / prev_close * 100.0).dropna()
        out.update({
            "day_candles": int(len(day)),
            "avg_day_range_pct": round(float(range_pct.mean()), 2),
            "avg_abs_gap_pct": round(float(gap_pct.abs().mean()), 2),
            "gap_over_half_pct": round(
                100.0 * float((gap_pct.abs() >= GAP_THRESHOLD_PCT).mean()), 1),
        })
    return out


def xau_block(m1: pd.DataFrame) -> Optional[dict]:
    if m1 is None or len(m1) == 0:
        return None
    hourly = _hourly_travel(m1["ts"], m1["open"], m1["close"])
    utc_day = pd.to_datetime(m1["ts"], unit="s").dt.date
    by_day = m1.groupby(utc_day)
    day_range_pct = ((by_day["high"].max() - by_day["low"].min())
                     / by_day["close"].last() * 100.0)
    return {
        "days": int(utc_day.nunique()),
        "bars": int(len(m1)),
        "hourly_travel": hourly,
        "busiest_hours_ist": _busiest(hourly),
        "evening_share_pct": _evening_share(hourly),
        "avg_day_range_pct": round(float(day_range_pct.mean()), 2),
    }


def clock_agreement(mcx: Optional[dict], xau: Optional[dict]) -> Optional[dict]:
    """Do the two venues share the same busiest hours? The cross-venue check
    that made gold worth two data sources in the first place."""
    if not mcx or not xau:
        return None
    a, b = set(mcx["busiest_hours_ist"]), set(xau["busiest_hours_ist"])
    shared = sorted(a & b)
    return {
        "mcx_top3": mcx["busiest_hours_ist"],
        "xau_top3": xau["busiest_hours_ist"],
        "shared": shared,
        "verdict": ("same clock" if len(shared) >= 2
                    else "clocks differ — investigate before trusting either"),
    }


def build(m3: pd.DataFrame, day: pd.DataFrame, xau: pd.DataFrame) -> dict:
    mcx = mcx_block(m3, day)
    x = xau_block(xau)
    return {
        "mcx": mcx,
        "xauusd": x,
        "clock_agreement": clock_agreement(mcx, x),
        "note": (
            "Base rates published before any rule was graded — the priors that "
            "stop small-sample lies. Gold is an evening animal: 17:00–21:00 IST "
            "carries the bulk of intraday travel, and ~45% of days gap >0.5% "
            "(international trading continues while MCX sleeps), so any "
            "overnight idea is gap-exposed from birth. Session-shape labels "
            "measured mid-day are moment-context, never day-type."),
    }
