"""Shared helpers for strategy implementations."""
from __future__ import annotations

import math
from typing import Optional

from app.lab.engine import BarContext, StrategyBase
from app.lab.models import BUY, SELL, ExitPolicy, LegSpec, SignalIntent


def fut_policy(p: dict) -> ExitPolicy:
    """Standard futures bracket from the common futures params."""
    return ExitPolicy(
        stop_kind="atr_spine", stop_value=p.get("stop_atr"),
        target_kind="atr_spine", target_value=p.get("target_atr"),
        trail_kind="risk_pct" if p.get("trail_pct") else None,
        trail_value=p.get("trail_pct") or None,
    )


def prem_policy(p: dict) -> ExitPolicy:
    """Standard long-premium bracket from the common option params."""
    return ExitPolicy(
        stop_kind="pct_value", stop_value=p.get("stop_pct"),
        target_kind="pct_value", target_value=p.get("target_pct"),
        trail_kind="pct_value" if p.get("trail_pct") else None,
        trail_value=p.get("trail_pct") or None,
    )


def credit_policy(p: dict, stop_key: str = "credit_stop_pct") -> ExitPolicy:
    """Short-premium: stop when the loss reaches N% of the credit taken.
    No profit target by default — theta decay to squareoff IS the target."""
    return ExitPolicy(stop_kind="credit_pct", stop_value=p.get(stop_key,
                                                               p.get("stop_pct")))


def fut_leg(direction: str) -> LegSpec:
    return LegSpec(side=BUY if direction == "LONG" else SELL, right="FUT")


def opt_buy_leg(direction: str, p: dict, offset_key: str = "strike_offset"
                ) -> LegSpec:
    """Long CE for LONG, long PE for SHORT, at the configured offset."""
    return LegSpec(side=BUY, right="CE" if direction == "LONG" else "PE",
                   expiry_rule=p.get("expiry_rule", "weekly"),
                   strike_rule="ATM_OFF",
                   strike_offset=int(p.get(offset_key, 0)))


def signal(ctx: BarContext, direction: str, legs, policy: ExitPolicy,
           tag: str = "", note: str = "") -> SignalIntent:
    return SignalIntent(ts=ctx.ts, direction=direction,
                        legs=tuple(legs), exit_policy=policy, tag=tag,
                        note=note)


def nn(*vals) -> bool:
    """All values present and finite (features carry NaN during warmup)."""
    for v in vals:
        if v is None:
            return False
        try:
            if math.isnan(float(v)):
                return False
        except (TypeError, ValueError):
            return False
    return True


class OncePerDayMixin:
    """Track per-day one-shot flags (e.g. one attempt per breakout side)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._fired: set = set()

    def day_reset(self) -> None:
        self._fired = set()

    def fired(self, key: str) -> bool:
        return key in self._fired

    def mark(self, key: str) -> None:
        self._fired.add(key)
