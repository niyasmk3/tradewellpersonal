"""Orchestration for the Patterns Module: sync -> analyze -> persist results."""
from __future__ import annotations

import logging

from app.patterns import store
from app.patterns.analysis import (
    auction_print,
    build_daily,
    day_of_week_stats,
    pattern_frequency,
    time_of_day_profile,
)
from app.patterns.data import sync
from app.patterns.levels import find_levels
from app.patterns.tendencies import conditional_outcomes, exclude_current_day, volume_pace_curve

log = logging.getLogger("tradewell.patterns")

MIN_BARS = 1000  # ~2 weeks; below this every stat is noise


class PatternsError(RuntimeError):
    pass


def run_sync(kite, years: int = 3) -> dict:
    if kite is None:
        raise PatternsError("Kite is not authenticated — log in first")
    return sync(kite, years=years)


def run_analysis() -> dict:
    df = store.load_frame()
    if len(df) < MIN_BARS:
        raise PatternsError(
            f"Only {len(df)} bars stored — run a sync first (need >= {MIN_BARS})")

    daily = build_daily(df)
    results = {
        "disclaimer": (
            "Historical frequencies, not predictions. Every tendency below is a "
            "sample statistic with its n shown; treat anything near 50% as noise. "
            "Educational analysis, not financial advice."
        ),
        "data": {
            "bars": int(len(df)),
            "days": int(len(daily)),
            "from": daily["date"].iloc[0],
            "to": daily["date"].iloc[-1],
            "last_close": float(df["close"].iloc[-1]),
            "volume_note": (
                "NSE indices report no volume; vol_proxy is NIFTYBEES ETF 5-min "
                "volume, valid for relative time-of-day shape only."
            ),
            "has_volume_proxy": bool(df["vol_proxy"].sum() > 0),
        },
        "day_of_week": day_of_week_stats(daily),
        "time_of_day": time_of_day_profile(df),
        "candlestick_frequency": pattern_frequency(df),
        # Volume-conditioned pattern outcomes (15/30/60m) + the typical
        # cumulative-volume curve — the dataset the /patterns/live-read
        # endpoint joins today's tape against. The CURRENT day is excluded
        # (review catch): a mid-session re-analyze must not grade this
        # morning's patterns and then serve them back as independent
        # history this afternoon.
        "conditional_outcomes": conditional_outcomes(exclude_current_day(df)),
        "volume_pace": volume_pace_curve(exclude_current_day(df)),
        # The CAS auction-print ledger (05-Aug, user-spotted): official close
        # vs last free tape, daily — the expiry-settlement bias question.
        "auction_print": auction_print(df),
        "levels": find_levels(df),
    }
    store.save_results(results)
    log.info("Patterns analysis saved: %s bars, %s days", len(df), len(daily))
    return results


def status() -> dict:
    results = store.load_results()
    return {
        "bars": store.candle_count(),
        "last_sync": store.get_meta("last_sync"),
        "results_available": results is not None,
        "results_generated_at": results.get("generated_at") if results else None,
    }
