"""Reusable market features derived from candles + option chain.

Kept pure and side-effect free so the regime engine, the scorer, and the unit
tests can all share them. All functions tolerate short / empty input.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.models.schemas import OptionChain


@dataclass
class Structure:
    label: str          # "HH_HL" (uptrend), "LH_LL" (downtrend), "MIXED"
    recent_high: float | None
    recent_low: float | None
    swing_high: float | None   # prior swing high (breakout reference)
    swing_low: float | None


def market_structure(df: pd.DataFrame, lookback: int = 10) -> Structure:
    """Classify recent price structure via swing highs/lows over `lookback` bars."""
    if df is None or len(df) < 4:
        return Structure("MIXED", None, None, None, None)

    window = df.tail(lookback)
    highs = window["high"].to_numpy()
    lows = window["low"].to_numpy()
    half = max(2, len(window) // 2)

    first_high, last_high = highs[:half].max(), highs[half:].max()
    first_low, last_low = lows[:half].min(), lows[half:].min()

    higher_high = last_high > first_high
    higher_low = last_low > first_low
    lower_high = last_high < first_high
    lower_low = last_low < first_low

    if higher_high and higher_low:
        label = "HH_HL"
    elif lower_high and lower_low:
        label = "LH_LL"
    else:
        label = "MIXED"

    return Structure(
        label=label,
        recent_high=float(window["high"].max()),
        recent_low=float(window["low"].min()),
        swing_high=float(first_high),
        swing_low=float(first_low),
    )


def volume_ratio(df: pd.DataFrame, lookback: int = 20) -> float | None:
    """Last candle volume vs average of the previous `lookback` candles."""
    if df is None or len(df) < 3:
        return None
    vols = df["volume"]
    if vols.tail(lookback + 1).sum() == 0:
        return None  # e.g. index with no volume, or market closed
    avg = vols.iloc[-(lookback + 1):-1].mean()
    if not avg or avg <= 0:
        return None
    return float(vols.iloc[-1] / avg)


def opening_range(df: pd.DataFrame, bars: int = 5) -> tuple[float | None, float | None]:
    """Opening-range high/low from the first `bars` candles of the session."""
    if df is None or len(df) == 0:
        return None, None
    head = df.head(bars)
    return float(head["high"].max()), float(head["low"].min())


@dataclass
class OiAnalysis:
    pcr: float | None
    resistance_strike: float | None   # max CE OI
    support_strike: float | None      # max PE OI
    put_writing: float                # sum PE OI-build at/below ATM (bullish)
    call_writing: float               # sum CE OI-build at/above ATM (bearish)
    bias: str                         # "bullish" / "bearish" / "neutral"
    notes: list[str]


def oi_analysis(chain: OptionChain | None, spot: float | None) -> OiAnalysis:
    if chain is None or not chain.rows:
        return OiAnalysis(None, None, None, 0.0, 0.0, "neutral", ["no option-chain data"])

    atm = chain.atm_strike
    max_ce = max(chain.rows, key=lambda r: r.ce_oi or 0, default=None)
    max_pe = max(chain.rows, key=lambda r: r.pe_oi or 0, default=None)

    put_writing = 0.0
    call_writing = 0.0
    for r in chain.rows:
        if atm is not None:
            if r.strike <= atm and r.pe_oi_change:
                put_writing += max(0.0, r.pe_oi_change)      # puts written below ATM = support
            if r.strike >= atm and r.ce_oi_change:
                call_writing += max(0.0, r.ce_oi_change)     # calls written above ATM = resistance

    notes: list[str] = []
    bias = "neutral"
    pcr = chain.pcr
    # OI-build bias takes priority; fall back to PCR when there's no intraday delta.
    if put_writing > call_writing * 1.3 and put_writing > 0:
        bias = "bullish"
        notes.append("Net put writing (support building)")
    elif call_writing > put_writing * 1.3 and call_writing > 0:
        bias = "bearish"
        notes.append("Net call writing (resistance building)")
    elif pcr is not None:
        if pcr >= 1.15:
            bias = "bullish"
            notes.append(f"PCR {pcr} (put-heavy)")
        elif pcr <= 0.85:
            bias = "bearish"
            notes.append(f"PCR {pcr} (call-heavy)")

    if max_pe:
        notes.append(f"Support (max PE OI) at {max_pe.strike:.0f}")
    if max_ce:
        notes.append(f"Resistance (max CE OI) at {max_ce.strike:.0f}")

    return OiAnalysis(
        pcr=pcr,
        resistance_strike=max_ce.strike if max_ce else None,
        support_strike=max_pe.strike if max_pe else None,
        put_writing=put_writing,
        call_writing=call_writing,
        bias=bias,
        notes=notes,
    )
