"""Category D — option-chain & OI strategies (O01..O06). All depend on the
real chain archive; outside its window they are data-blocked by readiness."""
from __future__ import annotations

from typing import Optional

from app.lab.engine import BarContext, StrategyBase
from app.lab.models import BUY, SELL, LegSpec, SignalIntent
from app.lab.strategies.common import (
    OncePerDayMixin, credit_policy, nn, opt_buy_leg, prem_policy, signal,
)


def _doi_skew(ctx: BarContext, snaps_back: int) -> Optional[float]:
    """(dOI_PE - dOI_CE) / total-then, near ATM. Positive = put writing
    dominates (conventionally bullish support)."""
    if ctx.chain is None:
        return None
    now = ctx.chain.near_atm_oi()
    ago_f = ctx.chain_features_ago(snaps_back)
    if now is None or ago_f is None:
        return None
    ago = ago_f.near_atm_oi()
    if ago is None:
        return None
    base = ago["ce_oi"] + ago["pe_oi"]
    if base <= 0:
        return None
    d_ce = now["ce_oi"] - ago["ce_oi"]
    d_pe = now["pe_oi"] - ago["pe_oi"]
    return (d_pe - d_ce) / base


class O01_PcrExtremes(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if ctx.chain is None:
            return None
        pcr = ctx.chain.pcr()
        n = int(self.p["confirm_bars"])
        b = ctx.bars(n + 1)
        if pcr is None or len(b) < n + 1:
            return None
        move = float(b.iloc[-1]["close"]) - float(b.iloc[0]["close"])
        if pcr >= self.p["pcr_high"] and move > 0:
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="pcr_high_ce")
        if pcr <= self.p["pcr_low"] and move < 0:
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="pcr_low_pe")
        return None


class O02_OiBuildup(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        skew = _doi_skew(ctx, int(self.p["oi_window"]))
        if skew is None:
            return None
        b = ctx.bars(int(self.p["oi_window"]) + 1)
        if len(b) < 2:
            return None
        move = float(b.iloc[-1]["close"]) - float(b.iloc[0]["close"])
        if skew >= self.p["oi_skew_min"] and move > 0:
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="oi_build_ce")
        if skew <= -self.p["oi_skew_min"] and move < 0:
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="oi_build_pe")
        return None


class O03_OiWalls(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if ctx.chain is None:
            return None
        r = ctx.row
        if not nn(r.get("atr")):
            return None
        walls = ctx.chain.walls()
        close, atr = float(r["close"]), float(r["atr"])
        touch = self.p["touch_atr"] * atr
        pw, cw = walls.get("put_wall"), walls.get("call_wall")
        b = ctx.bars(2)
        holding_up = len(b) == 2 and b.iloc[1]["close"] >= b.iloc[0]["close"]
        holding_dn = len(b) == 2 and b.iloc[1]["close"] <= b.iloc[0]["close"]
        if pw and 0 <= close - pw <= touch and holding_up and \
                not self.fired("long"):
            self.mark("long")
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="put_wall_bounce")
        if cw and 0 <= cw - close <= touch and holding_dn and \
                not self.fired("short"):
            self.mark("short")
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="call_wall_fade")
        return None

    def wants_exit(self, ctx, pos) -> Optional[str]:
        if ctx.chain is None:
            return None
        walls = ctx.chain.walls()
        close = float(ctx.row["close"])
        if pos.tag == "put_wall_bounce" and walls.get("put_wall") and \
                close < walls["put_wall"]:
            return "wall_break"
        if pos.tag == "call_wall_fade" and walls.get("call_wall") and \
                close > walls["call_wall"]:
            return "wall_break"
        return None


class O04_DoiMomentum(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        w = int(self.p["mom_window"])
        skew = _doi_skew(ctx, w)
        if skew is None:
            return None
        b = ctx.bars(w + 1)
        if len(b) < 2:
            return None
        move = float(b.iloc[-1]["close"]) - float(b.iloc[0]["close"])
        if skew >= self.p["mom_min"] and move > 0:
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="doi_mom_ce")
        if skew <= -self.p["mom_min"] and move < 0:
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="doi_mom_pe")
        return None


class O05_StraddleRange(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        n = int(self.p["range_snaps"])
        series = ctx.straddle_series(n + 1)
        if len(series) < n // 2 + 2:
            return None
        cur = series[-1]
        hist = series[:-1]
        hi, lo = max(hist), min(hist)
        brk = self.p["break_pct"] / 100.0
        mode = self.p["mode"]
        if self.fired("done"):
            return None
        if mode in ("buy_break", "both") and cur >= hi * (1 + brk):
            self.mark("done")
            legs = [LegSpec(side=BUY, right="CE"),
                    LegSpec(side=BUY, right="PE")]
            return signal(ctx, "NEUTRAL", legs, prem_policy(self.p),
                          tag="straddle_break_buy")
        if mode in ("sell_decay", "both") and cur <= lo * (1 - brk):
            self.mark("done")
            legs = [LegSpec(side=SELL, right="CE"),
                    LegSpec(side=SELL, right="PE")]
            return signal(ctx, "NEUTRAL", legs, credit_policy(
                self.p, "stop_pct"), tag="straddle_decay_sell")
        return None


class O06_ChainReversal(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if ctx.chain is None:
            return None
        r = ctx.row
        if not nn(r.get("rsi"), r.get("atr")):
            return None
        pcr = ctx.chain.pcr()
        walls = ctx.chain.walls()
        if pcr is None:
            return None
        close, atr = float(r["close"]), float(r["atr"])
        pw, cw = walls.get("put_wall"), walls.get("call_wall")
        near_pw = pw is not None and abs(close - pw) <= atr
        near_cw = cw is not None and abs(close - cw) <= atr
        if near_pw and pcr >= self.p["pcr_high"] and \
                r["rsi"] <= self.p["rsi_low"]:
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="composite_rev_up")
        if near_cw and pcr <= self.p["pcr_low"] and \
                r["rsi"] >= self.p["rsi_high"]:
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="composite_rev_dn")
        return None


STRATEGIES = {
    "O01": O01_PcrExtremes, "O02": O02_OiBuildup, "O03": O03_OiWalls,
    "O04": O04_DoiMomentum, "O05": O05_StraddleRange,
    "O06": O06_ChainReversal,
}
