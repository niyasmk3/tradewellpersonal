"""Patterns Module (app/patterns) unit tests — synthetic bars, no Kite, no live files."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd

from app.patterns.analysis import build_daily, day_of_week_stats, pattern_frequency
from app.patterns.detect import PATTERNS, detect
from app.patterns.levels import find_levels, levels_near

IST = timezone(timedelta(hours=5, minutes=30))


def _bars(days=10, seed=7) -> pd.DataFrame:
    """Synthetic 5-min session bars: 75/day, 09:15-15:25 IST, random walk."""
    rng = np.random.default_rng(seed)
    rows = []
    price = 22000.0
    day = datetime(2025, 6, 2, tzinfo=IST)  # a Monday
    made = 0
    while made < days:
        if day.weekday() > 4:
            day += timedelta(days=1)
            continue
        t = day.replace(hour=9, minute=15)
        for _ in range(75):
            o = price
            c = o + rng.normal(0, 8)
            h = max(o, c) + abs(rng.normal(0, 4))
            l = min(o, c) - abs(rng.normal(0, 4))
            rows.append({"ts": int(t.timestamp()), "open": o, "high": h,
                         "low": l, "close": c, "vol_proxy": float(rng.integers(1000, 5000))})
            price = c
            t += timedelta(minutes=5)
        made += 1
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def test_detect_adds_all_pattern_columns_as_bool():
    det = detect(_bars(3))
    for name in PATTERNS:
        assert det[name].dtype == bool


def test_multi_candle_patterns_never_span_days():
    det = detect(_bars(4))
    dates = pd.to_datetime(det["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date
    first_bars = dates.ne(dates.shift(1))
    for name, (_, n_candles) in PATTERNS.items():
        if n_candles >= 2:
            assert not (det[name] & first_bars).any(), f"{name} fired on a session's first bar"


def test_reversal_patterns_respect_trend_gate():
    det = detect(_bars(6, seed=3))
    ema = det["close"].ewm(span=10, adjust=False).mean()
    below = (det["close"] < ema).shift(1, fill_value=False)
    # Hammers are downtrend-only by construction; every firing must sit where
    # the prior close was under its EMA.
    fired = det.index[det["hammer"]]
    for i in fired:
        assert below.iloc[i], "hammer fired without downtrend context"


def test_build_daily_and_weekday_stats():
    daily = build_daily(_bars(10))
    assert len(daily) == 10
    assert (daily["bars"] == 75).all()
    stats = day_of_week_stats(daily)
    # 10 straight weekdays from a Monday = 2 each; the first Monday is dropped
    # for lack of prev_close.
    assert sum(s["n_days"] for s in stats.values()) == 9
    for s in stats.values():
        assert 0.0 <= s["up_day_rate"] <= 1.0


def test_pattern_frequency_counts_match_detection():
    df = _bars(10)
    freq = pattern_frequency(df)
    det = detect(df)
    for name, entry in freq.items():
        assert entry["overall"]["count"] == int(det[name].sum())
        assert entry["overall"]["count"] == sum(v["count"] for v in entry["by_weekday"].values())


def test_levels_touch_counts_and_near_filter():
    res = find_levels(_bars(15))
    assert res["pivot_count"] > 0
    lv = res["levels"]
    assert lv, "random-walk sessions should still produce pivot clusters"
    for row in lv:
        assert row["days_touched"] <= row["total_touches"]
        assert row["as_resistance"] + row["as_support"] == row["total_touches"]
        assert row["held"] + row["broke"] <= row["total_touches"]
    near = levels_near(lv, lv[0]["level"], window_pct=1.0)
    assert lv[0] in near
