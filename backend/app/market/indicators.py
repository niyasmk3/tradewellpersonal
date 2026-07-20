"""Technical indicators computed from an intraday OHLCV candle DataFrame.

Hand-rolled with pandas/numpy (no TA-Lib) to keep the install dependency-light.
Every function is defensive about short input: it returns NaN-padded series so
the caller can always read ``.iloc[-1]`` safely, and ``compute_snapshot`` maps
those to ``None`` for the JSON layer.

The DataFrame is expected to hold **one trading day** of intraday candles for a
single instrument/timeframe, indexed 0..n with columns:
    open, high, low, close, volume
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.models.schemas import IndicatorSnapshot


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP. Assumes df is a single day's candles (resets naturally)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum()
    cum_pv = (typical * df["volume"]).cumsum()
    # Guard against zero volume at the open.
    return cum_pv / cum_vol.replace(0, np.nan)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder smoothing == ewm with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Wilder: zero average loss with gains present == RSI 100 (not undefined).
    # A totally flat series (both averages 0) stays NaN -> None downstream.
    return out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = _true_range(df)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """Return (supertrend_line, direction) where direction is 'up'/'down'.

    Standard band-flip implementation.
    """
    hl2 = (df["high"] + df["low"]) / 2.0
    atr_ = atr(df, period)
    upper_basic = hl2 + multiplier * atr_
    lower_basic = hl2 - multiplier * atr_

    n = len(df)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    st = np.full(n, np.nan)
    direction = ["up"] * n

    close = df["close"].to_numpy()
    ub = upper_basic.to_numpy()
    lb = lower_basic.to_numpy()

    for i in range(n):
        if i == 0 or np.isnan(ub[i]):
            final_upper[i] = ub[i]
            final_lower[i] = lb[i]
            st[i] = ub[i]
            direction[i] = "down"
            continue

        final_upper[i] = ub[i] if (ub[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = lb[i] if (lb[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]

        if st[i - 1] == final_upper[i - 1]:
            if close[i] <= final_upper[i]:
                st[i] = final_upper[i]
                direction[i] = "down"
            else:
                st[i] = final_lower[i]
                direction[i] = "up"
        else:
            if close[i] >= final_lower[i]:
                st[i] = final_lower[i]
                direction[i] = "up"
            else:
                st[i] = final_upper[i]
                direction[i] = "down"

    # Warmup mask: the recursion is seeded with an arbitrary 'down' at bar 0
    # (a pure uptrend still reads 'down' for its first ~5 bars — measured), so
    # the first `period` bars are noise, not signal. Mask them to NaN/None;
    # compute_snapshot maps those to None and the regime/scoring skip them.
    dirs: list = list(direction)
    for i in range(min(period, n)):
        st[i] = np.nan
        dirs[i] = None

    return pd.Series(st, index=df.index), pd.Series(dirs, index=df.index)


def bollinger_width(series: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.Series:
    middle = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return (upper - lower) / middle.replace(0, np.nan)


def _last(series: pd.Series) -> float | None:
    """Latest finite value of a series, or None."""
    if series is None or len(series) == 0:
        return None
    val = series.iloc[-1]
    if val is None or (isinstance(val, float) and (np.isnan(val) or np.isinf(val))):
        return None
    return round(float(val), 4)


def compute_snapshot(
    df: pd.DataFrame,
    prev_day_high: float | None = None,
    prev_day_low: float | None = None,
) -> IndicatorSnapshot:
    """Compute the latest value of every indicator for a candle DataFrame."""
    if df is None or len(df) == 0:
        return IndicatorSnapshot(prev_day_high=prev_day_high, prev_day_low=prev_day_low)

    close = df["close"]
    st_line, st_dir = supertrend(df)
    dir_val = st_dir.iloc[-1] if len(st_dir) else None

    return IndicatorSnapshot(
        vwap=_last(vwap(df)),
        ema9=_last(ema(close, 9)),
        ema20=_last(ema(close, 20)),
        ema50=_last(ema(close, 50)),
        rsi=_last(rsi(close)),
        atr=_last(atr(df)),
        adx=_last(adx(df)),
        supertrend=_last(st_line),
        supertrend_dir=dir_val if dir_val in ("up", "down") else None,
        bb_width=_last(bollinger_width(close)),
        prev_day_high=prev_day_high,
        prev_day_low=prev_day_low,
    )
