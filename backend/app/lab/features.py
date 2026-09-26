"""Causal feature columns for a spine frame (index or futures bars).

Everything here is a rolling/anchored function of PAST-OR-CURRENT bars only:
column value at row i may use rows <= i and nothing else. That property is
what lets the engine precompute features once and still replay honestly —
and it is pinned by tests/test_lab_engine.py, so keep any new column causal.

Reuses the live indicator maths in app/market/indicators.py wherever the
function is already causal (all of them are — they are rolling functions).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.market import indicators as ind


def add_features(df: pd.DataFrame, ema_fast: int = 9, ema_slow: int = 21,
                 st_period: int = 10, st_mult: float = 3.0) -> pd.DataFrame:
    """Returns a copy with feature columns. Base columns required:
    ts, open, high, low, close, volume (volume may be a proxy)."""
    d = df.copy().reset_index(drop=True)
    ist = d["ts"] + 19800
    d["day"] = ist // 86400
    d["minute"] = (ist % 86400) // 60          # IST minute-of-day
    close = d["close"]

    d["ema_fast"] = ind.ema(close, ema_fast)
    d["ema_slow"] = ind.ema(close, ema_slow)
    d["ema20"] = ind.ema(close, 20)
    d["ema50"] = ind.ema(close, 50)
    d["rsi"] = ind.rsi(close)
    d["atr"] = ind.atr(d)
    d["adx"] = ind.adx(d)
    st_line, st_dir = ind.supertrend(d, period=st_period, multiplier=st_mult)
    # indicators.supertrend speaks 'up'/'down' (None during warm-up) —
    # pinned by test_supertrend_direction_is_mapped, because a wrong map
    # here silently reads as "flat forever" and kills every ST strategy.
    d["st_dir"] = pd.Series(st_dir, index=d.index).map(
        {"up": 1, "down": -1}).fillna(0)
    d["bb_width"] = ind.bollinger_width(close)

    # Session VWAP, re-anchored each IST day. Zero-volume days (index proxy
    # gaps) fall back to typical price cumulative mean.
    tp = (d["high"] + d["low"] + d["close"]) / 3.0
    pv = tp * d["volume"]
    cum_v = d.groupby("day")["volume"].cumsum()
    cum_pv = pv.groupby(d["day"]).cumsum()
    vwap = np.where(cum_v > 0, cum_pv / cum_v.replace(0, np.nan),
                    tp.groupby(d["day"]).expanding().mean()
                    .reset_index(level=0, drop=True))
    d["vwap"] = vwap

    # Day context. Prev-day values must come from completed sessions only.
    grp = d.groupby("day")
    d["day_open"] = grp["open"].transform("first")
    d["day_high"] = grp["high"].cummax()
    d["day_low"] = grp["low"].cummin()
    day_last_close = grp["close"].last()
    day_high_full = grp["high"].max()
    day_low_full = grp["low"].min()
    prev_map_close = day_last_close.shift(1)
    prev_map_high = day_high_full.shift(1)
    prev_map_low = day_low_full.shift(1)
    d["prev_close"] = d["day"].map(prev_map_close)
    d["prev_high"] = d["day"].map(prev_map_high)
    d["prev_low"] = d["day"].map(prev_map_low)
    d["gap_pct"] = (d["day_open"] - d["prev_close"]) / d["prev_close"] * 100.0

    # Percentile ranks over trailing windows (causal by construction).
    d["atr_pctile"] = _trailing_pctile(d["atr"], 375)      # ~5 sessions of 5m
    d["bbw_pctile"] = _trailing_pctile(d["bb_width"], 375)

    # Volume z vs same-minute trailing baseline (breakout confirmation).
    d["vol_z"] = _minute_volume_z(d)
    return d


def _trailing_pctile(s: pd.Series, window: int) -> pd.Series:
    def rank_last(a: np.ndarray) -> float:
        last = a[-1]
        if np.isnan(last):
            return np.nan
        return float((a <= last).mean() * 100.0)
    return s.rolling(window, min_periods=window // 3).apply(rank_last,
                                                            raw=True)


def _minute_volume_z(d: pd.DataFrame, lookback_days: int = 10) -> pd.Series:
    """z-score of this bar's volume vs the SAME minute-of-day over the
    trailing `lookback_days` sessions (shifted: today never scores itself)."""
    out = np.full(len(d), np.nan)
    hist: dict = {}
    for i in range(len(d)):
        m = int(d["minute"].iloc[i])
        v = float(d["volume"].iloc[i])
        past = hist.get(m)
        if past and len(past) >= 5:
            arr = np.asarray(past, dtype=float)
            sd = arr.std()
            if sd > 0:
                out[i] = (v - arr.mean()) / sd
        hist.setdefault(m, []).append(v)
        if len(hist[m]) > lookback_days:
            hist[m].pop(0)
    return pd.Series(out, index=d.index)


# --- six-bucket regime (shared by engine, analytics and paper loop) -------

TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
SIDEWAYS = "sideways"
HIGH_VOL = "high_vol"
LOW_VOL = "low_vol"
UNCERTAIN = "uncertain"

REGIMES = (TREND_UP, TREND_DOWN, SIDEWAYS, HIGH_VOL, LOW_VOL, UNCERTAIN)


def classify_row(row, vix_pctile=None) -> str:
    """One label per bar from causal features. Priority order (documented,
    deliberate): extreme volatility trumps direction; clear trend beats
    quiet; low-vol squeeze beats generic sideways; a bar that fits nothing
    cleanly is UNCERTAIN rather than force-labelled."""
    adx = row.get("adx")
    atr_p = row.get("atr_pctile")
    bbw_p = row.get("bbw_pctile")
    if adx is None or (isinstance(adx, float) and np.isnan(adx)):
        return UNCERTAIN
    high_vol = (atr_p is not None and not np.isnan(atr_p) and atr_p >= 85) or \
               (vix_pctile is not None and vix_pctile >= 85)
    if high_vol:
        return HIGH_VOL
    up = row["ema20"] > row["ema50"] and row["close"] > row["vwap"]
    dn = row["ema20"] < row["ema50"] and row["close"] < row["vwap"]
    if adx >= 20 and up:
        return TREND_UP
    if adx >= 20 and dn:
        return TREND_DOWN
    low_vol = (bbw_p is not None and not np.isnan(bbw_p) and bbw_p <= 20) and \
              (atr_p is not None and not np.isnan(atr_p) and atr_p <= 30)
    if low_vol:
        return LOW_VOL
    if adx < 16:
        return SIDEWAYS
    return UNCERTAIN


def regime_allows(regime: str, filter_name: str) -> bool:
    if filter_name == "off":
        return True
    if filter_name == "trend_only":
        return regime in (TREND_UP, TREND_DOWN)
    if filter_name == "range_only":
        return regime in (SIDEWAYS, LOW_VOL)
    if filter_name == "exclude_highvol":
        return regime != HIGH_VOL
    return True


def session_bucket(minute: int) -> str:
    if minute < 11 * 60:
        return "open"
    if minute < 14 * 60:
        return "mid"
    return "close"
