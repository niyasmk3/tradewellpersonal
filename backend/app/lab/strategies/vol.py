"""Category E — volatility & expiry strategies (V01..V05)."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from app.lab.engine import BarContext, StrategyBase
from app.lab.fills import Quote, _delta_for
from app.lab.models import BUY, SELL, ExitPolicy, LegSpec, SignalIntent
from app.lab.strategies.common import (
    OncePerDayMixin, credit_policy, nn, prem_policy, signal,
)
from app.market.calendar import IST


def _sell(right: str, offset: int, p: dict, expiry: str = "weekly") -> LegSpec:
    return LegSpec(side=SELL, right=right, expiry_rule=expiry,
                   strike_rule="ATM_OFF", strike_offset=offset)


def _buy(right: str, offset: int, p: dict, expiry: str = "weekly") -> LegSpec:
    return LegSpec(side=BUY, right=right, expiry_rule=expiry,
                   strike_rule="ATM_OFF", strike_offset=offset)


class V01_VixRegime(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if self.fired("done") or ctx.vix_pctile is None:
            return None
        r = ctx.row
        if ctx.vix_pctile >= self.p["sell_pctile"] and \
                ctx.regime in ("sideways", "low_vol"):
            self.mark("done")
            off = int(self.p["strike_offset"])
            return signal(ctx, "NEUTRAL",
                          [_sell("CE", off, self.p), _sell("PE", off, self.p)],
                          credit_policy(self.p, "stop_pct"),
                          tag="vix_rich_sell")
        bbw = r.get("bbw_pctile")
        if ctx.vix_pctile <= self.p["buy_pctile"] and nn(bbw) and \
                float(bbw) <= 20:
            self.mark("done")
            return signal(ctx, "NEUTRAL",
                          [_buy("CE", 0, self.p), _buy("PE", 0, self.p)],
                          prem_policy(self.p), tag="vix_cheap_buy")
        return None


class V02_IvRv(OncePerDayMixin, StrategyBase):
    def _realized_vol(self, ctx: BarContext) -> Optional[float]:
        days = int(self.p["rv_days"])
        b = ctx.bars(days * 75 + 1)
        if len(b) < days * 40:
            return None
        rets = b["close"].pct_change().dropna()
        if len(rets) < 50:
            return None
        # annualised from 5-min bars: 75 bars/session, 252 sessions
        return float(rets.std() * math.sqrt(75 * 252) * 100.0)

    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if self.fired("done") or ctx.chain is None:
            return None
        iv = ctx.chain.atm_iv()
        rv = self._realized_vol(ctx)
        if iv is None or rv is None:
            return None
        spread = iv - rv
        if spread >= self.p["spread_min"]:
            self.mark("done")
            return signal(ctx, "NEUTRAL",
                          [_sell("CE", 0, self.p), _sell("PE", 0, self.p)],
                          credit_policy(self.p, "stop_pct"),
                          tag="iv_rich")
        if spread <= -self.p["spread_min"]:
            self.mark("done")
            return signal(ctx, "NEUTRAL",
                          [_buy("CE", 0, self.p), _buy("PE", 0, self.p)],
                          prem_policy(self.p), tag="iv_cheap")
        return None


class V03_Calendar(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if self.fired("done") or ctx.chain is None:
            return None
        r = ctx.row
        if int(r["minute"]) < int(self.p["entry_min"]):
            return None
        wk = ctx.chain.weekly_expiry()
        mo = ctx.chain.monthly_expiry()
        if not wk or not mo or wk == mo:
            return None        # expiry week: no calendar to trade
        w_straddle = ctx.chain.straddle()
        if w_straddle is None:
            return None
        # monthly straddle at the weekly ATM strike
        spot = float(r["close"])
        wk_chain_atm = ctx.chain.quote(wk, self._atm(ctx, wk, spot), "CE")
        if wk_chain_atm is None:
            return None
        strike = wk_chain_atm["strike"]
        mce = ctx.chain.quote(mo, strike, "CE")
        mpe = ctx.chain.quote(mo, strike, "PE")
        if not mce or not mpe:
            return None
        m_straddle = mce["ltp"] + mpe["ltp"]
        if m_straddle <= 0 or w_straddle / m_straddle < self.p["ratio_min"]:
            return None
        self.mark("done")
        legs = [_sell("CE", 0, self.p, "weekly"),
                _sell("PE", 0, self.p, "weekly"),
                _buy("CE", 0, self.p, "monthly"),
                _buy("PE", 0, self.p, "monthly")]
        pol = ExitPolicy(stop_kind="pct_value",
                         stop_value=self.p.get("stop_pct"),
                         target_kind="pct_value",
                         target_value=self.p.get("target_pct"))
        return signal(ctx, "NEUTRAL", legs, pol, tag="calendar")

    @staticmethod
    def _atm(ctx: BarContext, expiry: str, spot: float) -> float:
        return round(spot / 50.0) * 50.0

    def wants_exit(self, ctx, pos) -> Optional[str]:
        # exit the morning of the short leg's expiry, or after max hold
        short_exp = next((l.expiry for l in pos.legs if l.side == SELL), None)
        if short_exp and ctx.day >= short_exp and \
                int(ctx.row["minute"]) >= 600:
            return "pre_expiry"
        entry_day = datetime.fromtimestamp(pos.entry_ts, IST).date()
        cur_day = datetime.fromtimestamp(ctx.ts, IST).date()
        if (cur_day - entry_day).days >= int(self.p["hold_days_max"]):
            return "max_hold_days"
        return None


class V04_ExpiryDecay(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if not ctx.expiry_day or self.fired("done"):
            return None
        if int(ctx.row["minute"]) < int(self.p["entry_min"]):
            return None
        self.mark("done")
        off = int(self.p["strike_offset"])
        return signal(ctx, "NEUTRAL",
                      [_sell("CE", off, self.p), _sell("PE", off, self.p)],
                      credit_policy(self.p, "stop_pct"), tag="expiry_decay")


class V05_GammaScalp(OncePerDayMixin, StrategyBase):
    def __init__(self, sdef, params):
        super().__init__(sdef, params)
        self._hedges = 0

    def day_reset(self):
        super().day_reset()
        self._hedges = 0

    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if self.fired("done"):
            return None
        if int(ctx.row["minute"]) < int(self.p["entry_min"]):
            return None
        self.mark("done")
        return signal(ctx, "NEUTRAL",
                      [_buy("CE", 0, self.p), _buy("PE", 0, self.p)],
                      prem_policy(self.p), tag="gamma_scalp")

    def _net_delta_lots(self, ctx: BarContext, pos) -> Optional[float]:
        if ctx.chain is None:
            return None
        total_units = 0.0
        lot = None
        for idx, leg in enumerate(pos.legs):
            if str(idx) in pos.exit_prices:
                continue
            sign = 1.0 if leg.side == BUY else -1.0
            if leg.right == "FUT":
                total_units += sign * leg.qty
                lot = lot or leg.lot_size
                continue
            qd = ctx.chain.quote(leg.expiry, leg.strike, leg.right)
            if qd is None:
                return None
            q = Quote(token=qd["token"], tradingsymbol="", expiry=qd["expiry"],
                      strike=qd["strike"], right=leg.right, ltp=qd["ltp"],
                      bid=qd["bid"], ask=qd["ask"], oi=qd["oi"],
                      ts=ctx.chain.snap_ts or ctx.ts, iv=qd["iv"])
            dlt = _delta_for(q, ctx.chain.spot, ctx.ts)
            if dlt is None:
                return None
            total_units += sign * dlt * leg.qty
            lot = lot or leg.lot_size
        if lot is None or lot <= 0:
            return None
        return total_units / lot

    def adjust(self, ctx: BarContext, pos) -> Optional[list]:
        if self._hedges >= int(self.p["max_hedges"]):
            return None
        net = self._net_delta_lots(ctx, pos)
        if net is None:
            return None
        if net >= float(self.p["hedge_delta"]):
            self._hedges += 1
            return [("open", LegSpec(side=SELL, right="FUT"))]
        if net <= -float(self.p["hedge_delta"]):
            self._hedges += 1
            return [("open", LegSpec(side=BUY, right="FUT"))]
        return None


STRATEGIES = {
    "V01": V01_VixRegime, "V02": V02_IvRv, "V03": V03_Calendar,
    "V04": V04_ExpiryDecay, "V05": V05_GammaScalp,
}
