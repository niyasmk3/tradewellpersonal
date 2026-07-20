"""Candlestick-pattern detection — used as a *confirmation feature*, not a trigger.

Geometry and thresholds follow the bundled candlestick-patterns skill's
objective rules (body/range/shadow math, sizes relative to a rolling reference,
trend gating). Per that skill's own evidence review, intraday candlestick
patterns have ~coin-flip standalone hit rates and edges that vanish after costs
— so here they only nudge the existing price-action score (capped) and add a
human-readable reason. They never generate or override a signal.

Detected on the underlying FUTURE's candles (real OHLC), never on option premium.
Only the high-frequency, non-gap-dependent patterns are included.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Pattern:
    name: str
    direction: str          # "bullish" | "bearish" | "neutral"
    note: str


def _geom(o: float, h: float, low: float, c: float):
    body = abs(c - o)
    rng = h - low
    upper = h - max(o, c)
    lower = min(o, c) - low
    return body, rng, upper, lower


def detect(df: pd.DataFrame) -> list[Pattern]:
    """Patterns whose signal candle is the last row of `df`.

    Sizes are judged relative to the rolling median range of the last ~20 bars,
    so the same rules hold across volatility regimes (per the skill).
    """
    if df is None or len(df) < 5:
        return []

    ref = float((df["high"] - df["low"]).tail(20).median())
    if ref <= 0:
        return []

    o, h, low, c = (float(df["open"].iloc[-1]), float(df["high"].iloc[-1]),
                    float(df["low"].iloc[-1]), float(df["close"].iloc[-1]))
    body, rng, upper, lower = _geom(o, h, low, c)
    if rng <= 0:
        return []

    po, ph, pl, pc = (float(df["open"].iloc[-2]), float(df["high"].iloc[-2]),
                      float(df["low"].iloc[-2]), float(df["close"].iloc[-2]))
    p_body = abs(pc - po)

    out: list[Pattern] = []
    bull_candle = c > o
    bear_candle = c < o

    # --- single-candle ---
    # Doji: near-zero body relative to its own range.
    if body <= 0.1 * rng:
        out.append(Pattern("Doji", "neutral", "indecision"))

    # Hammer (bullish rejection): long lower shadow, tiny upper, body up top.
    elif lower >= 2 * body and upper <= 0.25 * body and max(o, c) >= low + 0.7 * rng:
        out.append(Pattern("Hammer", "bullish", "lower-wick rejection"))

    # Shooting star (bearish rejection): long upper shadow, tiny lower, body down low.
    elif upper >= 2 * body and lower <= 0.25 * body and min(o, c) <= low + 0.3 * rng:
        out.append(Pattern("Shooting Star", "bearish", "upper-wick rejection"))

    # Marubozu: large body, negligible wicks, above-median range (momentum).
    elif body >= 0.9 * rng and rng >= ref:
        if bull_candle:
            out.append(Pattern("Bullish Marubozu", "bullish", "full-body momentum"))
        elif bear_candle:
            out.append(Pattern("Bearish Marubozu", "bearish", "full-body momentum"))

    # --- two-candle engulfing (body fully engulfs prior body) ---
    if bull_candle and pc < po and o <= pc and c >= po and body > p_body:
        out.append(Pattern("Bullish Engulfing", "bullish", "buyers engulfed prior candle"))
    elif bear_candle and pc > po and o >= pc and c <= po and body > p_body:
        out.append(Pattern("Bearish Engulfing", "bearish", "sellers engulfed prior candle"))

    # --- three-candle star (needs a small-bodied middle 'star') ---
    if len(df) >= 3:
        o1, c1 = float(df["open"].iloc[-3]), float(df["close"].iloc[-3])
        b1 = abs(c1 - o1)
        mid1 = (o1 + c1) / 2
        star_small = p_body <= 0.5 * b1 and b1 >= 0.5 * ref
        if star_small and c1 < o1 and bull_candle and c >= mid1 and body >= 0.5 * ref:
            out.append(Pattern("Morning Star", "bullish", "downtrend → reversal up"))
        elif star_small and c1 > o1 and bear_candle and c <= mid1 and body >= 0.5 * ref:
            out.append(Pattern("Evening Star", "bearish", "uptrend → reversal down"))

    return out


def confirmation(df: pd.DataFrame, bullish: bool, near_vwap: bool) -> tuple[float, list[str]]:
    """Confirmation delta (capped, small) + reasons for a candidate direction.

    Rewards a same-direction pattern (extra when it sits at a VWAP/EMA pullback),
    flags a conflicting pattern or a doji as caution. Bounded to keep candlesticks
    a modest feature, never a driver.
    """
    patterns = detect(df)
    if not patterns:
        return 0.0, []

    want = "bullish" if bullish else "bearish"
    opp = "bearish" if bullish else "bullish"
    delta = 0.0
    reasons: list[str] = []

    for p in patterns:
        if p.direction == want:
            gain = 3.0 if near_vwap else 2.0
            delta += gain
            loc = " at VWAP/EMA" if near_vwap else ""
            reasons.append(f"{p.name}{loc} ({p.note})")
        elif p.direction == opp:
            delta -= 2.0
            reasons.append(f"{p.name} — caution ({p.note})")
        else:  # doji / indecision
            delta -= 1.0
            reasons.append(f"{p.name} — indecision")

    # Clamp the candlestick contribution to a small band.
    delta = max(-3.0, min(4.0, delta))
    return delta, reasons[:2]
