"""Category C — options selling (S01..S08). Multi-leg, defined- and
undefined-risk; the risk engine and margin estimates cap exposure, and the
engine's credit_pct stop is the loss brake."""
from __future__ import annotations

from typing import Optional

from app.lab.engine import BarContext, StrategyBase
from app.lab.models import BUY, SELL, LegSpec, SignalIntent
from app.lab.strategies.common import (
    OncePerDayMixin, credit_policy, nn, signal,
)


def _sell(right: str, offset: int, p: dict) -> LegSpec:
    return LegSpec(side=SELL, right=right,
                   expiry_rule=p.get("expiry_rule", "weekly"),
                   strike_rule="ATM_OFF", strike_offset=offset)


def _buy(right: str, offset: int, p: dict) -> LegSpec:
    return LegSpec(side=BUY, right=right,
                   expiry_rule=p.get("expiry_rule", "weekly"),
                   strike_rule="ATM_OFF", strike_offset=offset)


class _TimedEntry(OncePerDayMixin, StrategyBase):
    """Enter once per day at/after entry_min. Subclasses build legs."""

    tag = "short_premium"

    def _ready(self, ctx: BarContext) -> bool:
        return int(ctx.row["minute"]) >= int(self.p["entry_min"]) and \
            not self.fired("done")

    def _legs(self, ctx: BarContext) -> Optional[list]:
        raise NotImplementedError

    def _policy(self):
        return credit_policy(self.p)

    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if not self._ready(ctx):
            return None
        legs = self._legs(ctx)
        if not legs:
            return None
        self.mark("done")
        return signal(ctx, "NEUTRAL", legs, self._policy(), tag=self.tag)


class S01_Straddle(_TimedEntry):
    tag = "atm_straddle"

    def _legs(self, ctx):
        return [_sell("CE", 0, self.p), _sell("PE", 0, self.p)]

    def adjust(self, ctx: BarContext, pos) -> Optional[list]:
        leg_stop = float(self.p.get("leg_stop_pct") or 0)
        if leg_stop <= 0 or ctx.chain is None:
            return None
        acts = []
        for idx, leg in enumerate(pos.legs):
            if str(idx) in pos.exit_prices or leg.side != SELL:
                continue
            q = ctx.chain.quote(leg.expiry, leg.strike, leg.right)
            if q and q["ltp"] >= leg.price * (1 + leg_stop / 100.0):
                acts.append(("close", idx))
        return acts or None


class S02_Strangle(_TimedEntry):
    tag = "otm_strangle"

    def _legs(self, ctx):
        off = int(self.p["strike_offset"])
        return [_sell("CE", off, self.p), _sell("PE", off, self.p)]


class S03_DynamicStraddle(_TimedEntry):
    tag = "dyn_straddle"

    def __init__(self, sdef, params):
        super().__init__(sdef, params)
        self._rolls = 0

    def day_reset(self):
        super().day_reset()
        self._rolls = 0

    def _legs(self, ctx):
        return [_sell("CE", 0, self.p), _sell("PE", 0, self.p)]

    def adjust(self, ctx: BarContext, pos) -> Optional[list]:
        if self._rolls >= int(self.p["max_rolls"]):
            return None
        open_short = [(i, l) for i, l in enumerate(pos.legs)
                      if str(i) not in pos.exit_prices and l.side == SELL]
        if not open_short:
            return None
        strike = open_short[0][1].strike
        drift = abs(float(ctx.row["close"]) - strike)
        if drift < int(self.p["recenter_steps"]) * 50.0:
            return None
        self._rolls += 1
        acts = [("close", i) for i, _ in open_short]
        acts.append(("open", _sell("CE", 0, self.p)))
        acts.append(("open", _sell("PE", 0, self.p)))
        return acts


class S04_IronCondor(_TimedEntry):
    tag = "iron_condor"

    def _legs(self, ctx):
        so, ww = int(self.p["short_offset"]), int(self.p["wing_width"])
        return [_sell("CE", so, self.p), _sell("PE", so, self.p),
                _buy("CE", so + ww, self.p), _buy("PE", so + ww, self.p)]


class S05_IronButterfly(_TimedEntry):
    tag = "iron_fly"

    def _legs(self, ctx):
        ww = int(self.p["wing_width"])
        return [_sell("CE", 0, self.p), _sell("PE", 0, self.p),
                _buy("CE", ww, self.p), _buy("PE", ww, self.p)]


class _TrendSpread(OncePerDayMixin, StrategyBase):
    """S06/S07 shared: trend filter + defined-risk credit spread."""

    bullish = True
    tag = "credit_spread"

    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if self.fired("done"):
            return None
        r = ctx.row
        if not nn(r.get("ema20"), r.get("ema50"), r.get("vwap")):
            return None
        up = r["ema20"] > r["ema50"] and r["close"] > r["vwap"]
        dn = r["ema20"] < r["ema50"] and r["close"] < r["vwap"]
        if (self.bullish and not up) or (not self.bullish and not dn):
            return None
        self.mark("done")
        so, ww = int(self.p["short_offset"]), int(self.p["wing_width"])
        right = "PE" if self.bullish else "CE"
        legs = [_sell(right, so, self.p), _buy(right, so + ww, self.p)]
        return signal(ctx, "LONG" if self.bullish else "SHORT", legs,
                      credit_policy(self.p), tag=self.tag)


class S06_BullPut(_TrendSpread):
    bullish = True
    tag = "bull_put"


class S07_BearCall(_TrendSpread):
    bullish = False
    tag = "bear_call"


class S08_IvRankSelling(_TimedEntry):
    tag = "iv_rank_strangle"

    def _legs(self, ctx):
        # base class only marks the day done AFTER legs build, so a low IV
        # rank keeps re-checking on later bars.
        pct = ctx.atm_iv_percentile(int(self.p["iv_lookback"]))
        if pct is None or pct < float(self.p["iv_rank_min"]):
            return None
        off = int(self.p["strike_offset"])
        return [_sell("CE", off, self.p), _sell("PE", off, self.p)]


STRATEGIES = {
    "S01": S01_Straddle, "S02": S02_Strangle, "S03": S03_DynamicStraddle,
    "S04": S04_IronCondor, "S05": S05_IronButterfly, "S06": S06_BullPut,
    "S07": S07_BearCall, "S08": S08_IvRankSelling,
}
