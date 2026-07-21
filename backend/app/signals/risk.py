"""Risk plan: underlying invalidation, premium stop-loss, targets, trailing.

Two independent stops per the spec:
  * Underlying invalidation — the more reliable one: exit the option if the
    index breaks the signal's swing level (candle-based).
  * Premium stop-loss — a percentage floor on the option premium itself.

Targets are risk-multiples of the premium stop distance.
"""
from __future__ import annotations

from app.models.schemas import IndicatorSnapshot
from app.signals.models import Direction, RiskPlan

_TICK = 0.05


def _round_tick(x: float) -> float:
    return round(round(x / _TICK) * _TICK, 2)


def build(
    direction: Direction,
    entry: float,
    df,
    ind: IndicatorSnapshot,
    spot: float,
    symbol: str,
    timeframe: str,
    premium_sl_pct: float,
    rr1: float,
    rr2: float,
    basis: float = 0.0,
    disaster_pct: float | None = None,
) -> RiskPlan:
    """`spot` and candle levels are in futures space; `basis` = future − index,
    so reported invalidation levels are converted to index (spot) terms."""
    ce = direction is Direction.CE

    # --- underlying invalidation (candle-based swing level) ---
    tail = df.tail(3)
    if ce:
        level = float(tail["low"].min())
        # VWAP just below price is often the tighter, more relevant stop.
        if ind.vwap is not None and level < ind.vwap < spot:
            level = ind.vwap
        level_idx = round(level - basis, 2)
        inval_dir = "above"
        underlying = f"{symbol} must stay above {level_idx:.0f}"
        note = f"Exit if {symbol} closes below {level_idx:.0f} on the {timeframe} candle"
    else:
        level = float(tail["high"].max())
        if ind.vwap is not None and spot < ind.vwap < level:
            level = ind.vwap
        level_idx = round(level - basis, 2)
        inval_dir = "below"
        underlying = f"{symbol} must stay below {level_idx:.0f}"
        note = f"Exit if {symbol} closes above {level_idx:.0f} on the {timeframe} candle"

    # --- premium stop + targets ---
    premium_sl = _round_tick(entry * (1 - premium_sl_pct))
    risk = max(entry - premium_sl, _TICK)
    target1 = _round_tick(entry + risk * rr1)
    target2 = _round_tick(entry + risk * rr2)

    entry_low = _round_tick(entry * 0.99)
    entry_high = _round_tick(entry * 1.02)

    # Backstop for when the index invalidation is the primary stop. Deliberately
    # NOT used to derive targets or the risk unit: at 45% a 1.5R target would sit
    # +68% away, which no intraday option move reaches.
    disaster_sl = _round_tick(entry * (1 - disaster_pct)) if disaster_pct else None

    return RiskPlan(
        entry_low=entry_low,
        entry_high=entry_high,
        premium_sl=premium_sl,
        disaster_sl=disaster_sl,
        target1=target1,
        target2=target2,
        trailing_sl_rule=f"Move SL to entry (₹{entry:.2f}) after Target 1",
        risk_reward=round(rr1, 2),
        underlying_invalidation=underlying,
        invalidation_note=note,
        invalidation_level=level_idx,
        invalidation_dir=inval_dir,
    )
