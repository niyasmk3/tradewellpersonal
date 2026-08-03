"""Objective candlestick-pattern detection on 5-minute bars (vectorized).

Geometry follows the literature-backed parameterization (Morris / Marshall et
al.): body and shadows are classified against a rolling 20-bar median range —
never absolute points — so the same rules hold at NIFTY 19k and 26k. Reversal
patterns are only counted when the classical trend context is present, coded
as close vs a 10-bar EMA (detectors without a trend gate flag mid-range noise
as "reversals", which would inflate every frequency count downstream).

Gap-dependent patterns (stars with true gaps, abandoned baby) are deliberately
excluded: consecutive 5-min bars almost never gap, so equity-textbook gap
rules would either never fire or need redefinition. Doji-body versions of
morning/evening star are used instead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TREND_EMA = 10
VOL_LOOKBACK = 20

# name -> (direction, min_candles). direction: +1 bullish signal, -1 bearish.
PATTERNS = {
    "doji": (0, 1),
    "spinning_top": (0, 1),
    "hammer": (1, 1),
    "inverted_hammer": (1, 1),
    "hanging_man": (-1, 1),
    "shooting_star": (-1, 1),
    "bull_marubozu": (1, 1),
    "bear_marubozu": (-1, 1),
    "bull_engulfing": (1, 2),
    "bear_engulfing": (-1, 2),
    "bull_harami": (1, 2),
    "bear_harami": (-1, 2),
    "piercing_line": (1, 2),
    "dark_cloud_cover": (-1, 2),
    "tweezer_bottom": (1, 2),
    "tweezer_top": (-1, 2),
    "morning_star": (1, 3),
    "evening_star": (-1, 3),
    "three_white_soldiers": (1, 3),
    "three_black_crows": (-1, 3),
    "inside_bar": (0, 2),
}


def detect(df: pd.DataFrame) -> pd.DataFrame:
    """Adds one boolean column per pattern to a copy of df (needs open/high/
    low/close). Bars are assumed contiguous within a session; patterns never
    span the overnight boundary (see session_id grouping)."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    out = df.copy()

    body = (c - o).abs()
    rng = (h - l).clip(lower=1e-9)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    bull = c > o
    bear = c < o

    med_rng = rng.rolling(VOL_LOOKBACK, min_periods=5).median()
    long_body = body >= 0.8 * med_rng
    ema = c.ewm(span=TREND_EMA, adjust=False).mean()
    # Trend context of the bar BEFORE the pattern's first candle is what the
    # classical definitions reference, hence the shift(1).
    downtrend = (c < ema).shift(1, fill_value=False)
    uptrend = (c > ema).shift(1, fill_value=False)

    # Day boundary: multi-candle patterns must not stitch yesterday's close to
    # today's open — that gap is a different animal (analyzed separately).
    session = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date
    same_day_1 = pd.Series(session, index=df.index).eq(pd.Series(session, index=df.index).shift(1))
    same_day_2 = same_day_1 & same_day_1.shift(1, fill_value=False)

    def prev(s, n=1):
        return s.shift(n, fill_value=False) if s.dtype == bool else s.shift(n)

    # --- single candle ---
    out["doji"] = body <= 0.1 * rng
    out["spinning_top"] = (body <= 0.35 * rng) & (upper >= body) & (lower >= body) & ~out["doji"]
    hammer_shape = (lower >= 2 * body) & (upper <= 0.25 * body.clip(lower=1e-9)) & (np.maximum(o, c) >= l + 0.7 * rng)
    istar_shape = (upper >= 2 * body) & (lower <= 0.25 * body.clip(lower=1e-9)) & (np.minimum(o, c) <= l + 0.3 * rng)
    out["hammer"] = hammer_shape & downtrend
    out["hanging_man"] = hammer_shape & uptrend
    out["inverted_hammer"] = istar_shape & downtrend
    out["shooting_star"] = istar_shape & uptrend
    maru = (body >= 0.9 * rng) & long_body
    out["bull_marubozu"] = maru & bull
    out["bear_marubozu"] = maru & bear

    # --- two candle ---
    engulf = (np.minimum(o, c) <= np.minimum(prev(o), prev(c))) & (
        np.maximum(o, c) >= np.maximum(prev(o), prev(c))) & (body > prev(body))
    out["bull_engulfing"] = engulf & bull & prev(bear) & downtrend & same_day_1
    out["bear_engulfing"] = engulf & bear & prev(bull) & uptrend & same_day_1
    harami = (np.maximum(o, c) <= np.maximum(prev(o), prev(c))) & (
        np.minimum(o, c) >= np.minimum(prev(o), prev(c))) & prev(long_body)
    out["bull_harami"] = harami & prev(bear) & downtrend & same_day_1
    out["bear_harami"] = harami & prev(bull) & uptrend & same_day_1
    mid_prev = (prev(o) + prev(c)) / 2
    out["piercing_line"] = (prev(bear) & bull & (o <= prev(c)) & (c > mid_prev) & (c < prev(o))
                            & downtrend & same_day_1)
    out["dark_cloud_cover"] = (prev(bull) & bear & (o >= prev(c)) & (c < mid_prev) & (c > prev(o))
                               & uptrend & same_day_1)
    tw_tol = 0.1 * med_rng
    out["tweezer_bottom"] = ((l - prev(l)).abs() <= tw_tol) & prev(bear) & bull & downtrend & same_day_1
    out["tweezer_top"] = ((h - prev(h)).abs() <= tw_tol) & prev(bull) & bear & uptrend & same_day_1
    out["inside_bar"] = (h <= prev(h)) & (l >= prev(l)) & same_day_1

    # --- three candle (doji-star variants: no gap requirement intraday) ---
    small_mid = prev(body) <= 0.3 * med_rng
    out["morning_star"] = (prev(bear, 2) & prev(long_body, 2) & small_mid & bull & long_body
                           & (c > (prev(o, 2) + prev(c, 2)) / 2)
                           & downtrend.shift(2, fill_value=False) & same_day_2)
    out["evening_star"] = (prev(bull, 2) & prev(long_body, 2) & small_mid & bear & long_body
                           & (c < (prev(o, 2) + prev(c, 2)) / 2)
                           & uptrend.shift(2, fill_value=False) & same_day_2)
    sold = bull & prev(bull) & prev(bull, 2) & (c > prev(c)) & (prev(c) > prev(c, 2)) & long_body & prev(long_body)
    out["three_white_soldiers"] = sold & downtrend.shift(2, fill_value=False) & same_day_2
    crow = bear & prev(bear) & prev(bear, 2) & (c < prev(c)) & (prev(c) < prev(c, 2)) & long_body & prev(long_body)
    out["three_black_crows"] = crow & uptrend.shift(2, fill_value=False) & same_day_2

    for name in PATTERNS:
        out[name] = out[name].fillna(False).astype(bool)
    return out
