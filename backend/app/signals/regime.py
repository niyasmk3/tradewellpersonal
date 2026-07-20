"""Market-regime classification.

A transparent vote model over the indicator stack (EMA alignment, VWAP,
Supertrend, RSI), price structure, and day direction — plus special-case
detection for volatility compression and news/volatility spikes. The regime
gates everything downstream: a non-tradeable regime (sideways / compression /
unsafe) short-circuits to a no-trade before any scoring happens.
"""
from __future__ import annotations

import numpy as np

from app.market.indicators import bollinger_width, atr as atr_series
from app.models.schemas import IndicatorSnapshot
from app.signals.features import market_structure
from app.signals.models import Bias, Regime, RegimeResult

_MIN_CANDLES = 15

_LABELS = {
    Regime.STRONG_BULLISH: "Strong Bullish",
    Regime.MODERATE_BULLISH: "Moderate Bullish",
    Regime.STRONG_BEARISH: "Strong Bearish",
    Regime.MODERATE_BEARISH: "Moderate Bearish",
    Regime.SIDEWAYS: "Sideways",
    Regime.COMPRESSION: "Volatility Compression",
    Regime.BREAKOUT_DEVELOPING: "Breakout Developing",
    Regime.NEWS_VOLATILITY: "News / Volatility",
    Regime.REVERSAL: "Reversal",
    Regime.UNSAFE: "Unsafe",
    Regime.WARMING_UP: "Warming Up",
}


def regime_label(regime: Regime) -> str:
    return _LABELS.get(regime, regime.value)


def classify(
    df,
    ind: IndicatorSnapshot,
    prev_close: float | None = None,
    vix_status: str | None = None,
    min_candles: int = _MIN_CANDLES,
) -> RegimeResult:
    if df is None or len(df) < min_candles:
        return RegimeResult(
            regime=Regime.WARMING_UP, bias=Bias.NEUTRAL, strength=0.0, tradeable=False,
            notes=[f"Warming up ({0 if df is None else len(df)}/{min_candles} candles)"],
        )

    price = float(df["close"].iloc[-1])
    adx = ind.adx or 0.0
    struct = market_structure(df)
    notes: list[str] = []

    # --- directional vote ---
    bull = bear = 0
    if ind.vwap is not None:
        if price > ind.vwap:
            bull += 1
        else:
            bear += 1
    if ind.ema9 is not None and ind.ema20 is not None:
        bull += ind.ema9 > ind.ema20
        bear += ind.ema9 < ind.ema20
    if ind.ema20 is not None and ind.ema50 is not None:
        bull += ind.ema20 > ind.ema50
        bear += ind.ema20 < ind.ema50
    if ind.supertrend_dir == "up":
        bull += 1
    elif ind.supertrend_dir == "down":
        bear += 1
    if struct.label == "HH_HL":
        bull += 1
    elif struct.label == "LH_LL":
        bear += 1
    if ind.rsi is not None:
        if ind.rsi > 55:
            bull += 1
        elif ind.rsi < 45:
            bear += 1
    if prev_close:
        if price > prev_close:
            bull += 1
        elif price < prev_close:
            bear += 1

    net = bull - bear

    # --- special regimes ---
    bbw = bollinger_width(df["close"])
    cur_bbw = bbw.iloc[-1] if len(bbw) else np.nan
    bbw_ref = bbw.tail(40).quantile(0.30)
    compression = (
        not np.isnan(cur_bbw) and not np.isnan(bbw_ref) and cur_bbw <= bbw_ref and adx < 20
    )

    atrs = atr_series(df)
    atr_med = atrs.tail(30).median()
    atr_spike = (
        len(atrs) > 5 and not np.isnan(atrs.iloc[-1]) and atr_med and atrs.iloc[-1] > 1.8 * atr_med
    )
    news_vol = atr_spike or vix_status == "High"

    if compression:
        notes.append("Bollinger bands compressed; ADX weak")
        return RegimeResult(regime=Regime.COMPRESSION, bias=Bias.NEUTRAL, strength=0.2,
                            tradeable=False, notes=notes + ["Wait for a breakout"])

    # Conflicting / rangebound
    if abs(net) <= 1 or adx < 16:
        notes.append(f"Directional vote {bull}/{bear}, ADX {adx:.0f}")
        if news_vol:
            return RegimeResult(regime=Regime.UNSAFE, bias=Bias.NEUTRAL, strength=0.1,
                                tradeable=False, notes=notes + ["Choppy + elevated volatility"])
        return RegimeResult(regime=Regime.SIDEWAYS, bias=Bias.NEUTRAL, strength=0.2,
                            tradeable=False, notes=notes + ["No clear edge"])

    bias = Bias.BULLISH if net > 0 else Bias.BEARISH
    strength = min(1.0, (abs(net) / 7.0) * 0.6 + min(adx, 40) / 40 * 0.4)
    notes.append(f"Vote {bull}/{bear}, ADX {adx:.0f}, structure {struct.label}")

    if news_vol:
        notes.append("Elevated volatility — confidence capped")
        return RegimeResult(regime=Regime.NEWS_VOLATILITY, bias=bias, strength=strength * 0.6,
                            tradeable=True, notes=notes)

    strong = abs(net) >= 4 and adx >= 23
    if bias is Bias.BULLISH:
        regime = Regime.STRONG_BULLISH if strong else Regime.MODERATE_BULLISH
    else:
        regime = Regime.STRONG_BEARISH if strong else Regime.MODERATE_BEARISH

    return RegimeResult(regime=regime, bias=bias, strength=strength, tradeable=True, notes=notes)
