"""Category B — options buying (B01..B08). Signals on the index spine,
fills against the real chain archive/live chain."""
from __future__ import annotations

from typing import Optional

from app.lab.engine import BarContext, StrategyBase
from app.lab.models import BUY, LegSpec, SignalIntent
from app.lab.strategies.common import (
    OncePerDayMixin, nn, opt_buy_leg, prem_policy, signal,
)

_SESSION_OPEN_MIN = 555


class _MomentumBurst(StrategyBase):
    """Shared engine for B01 (ATM) and B05 (delta-picked strike)."""

    def _leg(self, direction: str) -> LegSpec:
        return opt_buy_leg(direction, self.p)

    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        look = int(self.p["look_bars"])
        b = ctx.bars(look + 1)
        if len(b) < look + 1:
            return None
        r = b.iloc[-1]
        if not nn(r.get("atr")):
            return None
        if b["day"].nunique() > 1:
            return None
        move = float(r["close"]) - float(b.iloc[0]["close"])
        need = self.p["burst_atr"] * float(r["atr"])
        if move >= need:
            return signal(ctx, "LONG", [self._leg("LONG")],
                          prem_policy(self.p), tag="burst_up")
        if move <= -need:
            return signal(ctx, "SHORT", [self._leg("SHORT")],
                          prem_policy(self.p), tag="burst_dn")
        return None


class B01_AtmMomentum(_MomentumBurst):
    pass


class B02_OrbOption(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        r = ctx.row
        if not nn(r.get("atr")):
            return None
        orb_end = _SESSION_OPEN_MIN + int(self.p["orb_minutes"])
        if int(r["minute"]) < orb_end:
            return None
        day_bars = ctx.bars(90)
        day_bars = day_bars[day_bars["day"] == r["day"]]
        rng = day_bars[day_bars["minute"] < orb_end]
        if rng.empty:
            return None
        close = float(r["close"])
        if close > float(rng["high"].max()) and not self.fired("long"):
            self.mark("long")
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="orb_ce")
        if close < float(rng["low"].min()) and not self.fired("short"):
            self.mark("short")
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="orb_pe")
        return None


class B03_SupertrendOption(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        b = ctx.bars(2)
        if len(b) < 2:
            return None
        prev, cur = b.iloc[0], b.iloc[1]
        if not nn(cur["adx"]) or cur["adx"] < self.p["adx_min"]:
            return None
        if prev["st_dir"] <= 0 and cur["st_dir"] > 0:
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="st_ce")
        if prev["st_dir"] >= 0 and cur["st_dir"] < 0:
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="st_pe")
        return None

    def wants_exit(self, ctx, pos) -> Optional[str]:
        d = ctx.row.get("st_dir")
        if d is None:
            return None
        if pos.direction == "LONG" and d < 0:
            return "st_reverse"
        if pos.direction == "SHORT" and d > 0:
            return "st_reverse"
        return None


class B04_VwapBreakout(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        n = int(self.p["confirm_bars"])
        b = ctx.bars(n + 2)
        if len(b) < n + 2:
            return None
        r = b.iloc[-1]
        if not nn(r["vwap"], r["atr"]):
            return None
        if b["day"].nunique() > 1:
            return None
        # cross happened n bars ago, price held the new side since
        cross_bar = b.iloc[-(n + 1)]
        before = b.iloc[-(n + 2)]
        rng = float(cross_bar["high"] - cross_bar["low"])
        if rng < self.p["range_atr"] * float(r["atr"]):
            return None
        held = b.iloc[-n:]
        crossed_up = before["close"] <= before["vwap"] and \
            cross_bar["close"] > cross_bar["vwap"] and \
            (held["close"] > held["vwap"]).all()
        crossed_dn = before["close"] >= before["vwap"] and \
            cross_bar["close"] < cross_bar["vwap"] and \
            (held["close"] < held["vwap"]).all()
        if crossed_up:
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="vwap_break_ce")
        if crossed_dn:
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="vwap_break_pe")
        return None


class B05_DeltaStrike(_MomentumBurst):
    def _leg(self, direction: str) -> LegSpec:
        return LegSpec(side=BUY,
                       right="CE" if direction == "LONG" else "PE",
                       expiry_rule=self.p.get("expiry_rule", "weekly"),
                       strike_rule="DELTA",
                       delta_target=float(self.p["delta_target"]))


class B06_RsiReversal(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        b = ctx.bars(2)
        if len(b) < 2:
            return None
        prev, cur = b.iloc[0], b.iloc[1]
        if not nn(prev["rsi"], cur["rsi"]):
            return None
        lo, hi = self.p["rsi_low"], self.p["rsi_high"]
        if prev["rsi"] < lo <= cur["rsi"]:
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="rsi_snap_up")
        if prev["rsi"] > hi >= cur["rsi"]:
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="rsi_snap_dn")
        return None


class B07_BreakVolume(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        b = ctx.bars(2)
        if len(b) < 2:
            return None
        prev, cur = b.iloc[0], b.iloc[1]
        vz = cur.get("vol_z")
        if not nn(vz):
            return None
        if float(vz) < self.p["vol_z"]:
            return None
        # break of the day range as it stood BEFORE this bar
        if prev["day"] != cur["day"]:
            return None
        if cur["close"] > prev["day_high"] and not self.fired("long"):
            self.mark("long")
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="vol_break_ce")
        if cur["close"] < prev["day_low"] and not self.fired("short"):
            self.mark("short")
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="vol_break_pe")
        return None


class B08_ExpiryDirectional(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        if not ctx.expiry_day:
            return None
        r = ctx.row
        if int(r["minute"]) < int(self.p["start_min"]):
            return None
        if not nn(r.get("vwap")) or self.fired("done"):
            return None
        up = r["close"] > r["vwap"] and r["st_dir"] > 0
        dn = r["close"] < r["vwap"] and r["st_dir"] < 0
        if up:
            self.mark("done")
            return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                          prem_policy(self.p), tag="expiry_ce")
        if dn:
            self.mark("done")
            return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                          prem_policy(self.p), tag="expiry_pe")
        return None


STRATEGIES = {
    "B01": B01_AtmMomentum, "B02": B02_OrbOption,
    "B03": B03_SupertrendOption, "B04": B04_VwapBreakout,
    "B05": B05_DeltaStrike, "B06": B06_RsiReversal,
    "B07": B07_BreakVolume, "B08": B08_ExpiryDirectional,
}
