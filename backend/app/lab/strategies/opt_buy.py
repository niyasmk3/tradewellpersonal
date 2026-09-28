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


class B09_Vwap3Sigma(StrategyBase):
    """VWAP ±σ mean reversion (catalog B09). The v1 hypothesis is frozen:
    upper-band touch -> buy PE, lower-band touch -> buy CE, target = the
    underlying returning to session VWAP. Everything else (entry modes B/C,
    stop modes, targets, filters) is configuration layered on top — the
    engine's challenger machinery may tune those, never rewrite this.

    Episode discipline: a band touch DISARMS that side until price returns
    to VWAP (band-walking bars are one episode, not many signals); episode
    ordinals feed touch_policy and the trade tag (pe_e2_B = second upper
    episode, entry mode B)."""

    _UP, _DN = "up", "dn"

    def __init__(self, sdef, params):
        super().__init__(sdef, params)
        self._reset_day()

    def _reset_day(self):
        self._armed = {self._UP: True, self._DN: True}
        self._episodes = {self._UP: 0, self._DN: 0}
        self._pending = None    # mode C: dict(side, bars_left, touch_hi/lo)

    def day_reset(self) -> None:
        self._reset_day()

    # -- band maths ---------------------------------------------------------

    def _bands(self, row):
        v, sd = row.get("vwap"), row.get("vwap_sd")
        if not nn(v, sd) or float(sd) <= 0:
            return None
        m = float(self.p["sigma_mult"])
        return float(v), float(sd), float(v) + m * float(sd), \
            float(v) - m * float(sd)

    def _track(self, row):
        """Episode bookkeeping — run on EVERY bar (on_bar and wants_exit),
        so touches during an open position still count and re-arm right.
        Returns the side newly touched this bar, if any."""
        b = self._bands(row)
        if b is None:
            return None
        vwap, _sd, upper, lower = b
        # return-to-VWAP re-arms both sides
        if float(row["low"]) <= vwap <= float(row["high"]):
            self._armed[self._UP] = True
            self._armed[self._DN] = True
        touched = None
        if float(row["high"]) >= upper and self._armed[self._UP]:
            self._armed[self._UP] = False
            self._episodes[self._UP] += 1
            touched = self._UP
        if float(row["low"]) <= lower and self._armed[self._DN]:
            self._armed[self._DN] = False
            self._episodes[self._DN] += 1
            # simultaneous both-band touch on one bar: treat as no signal
            touched = self._DN if touched is None else None
        return touched

    def _episode_ok(self, side) -> bool:
        pol = self.p["touch_policy"]
        n = self._episodes[side]
        return n == 1 if pol == "first" else (n <= 2 if pol == "first_two"
                                              else True)

    def _slope_ok(self, ctx: BarContext, side) -> bool:
        mode = self.p["slope_filter"]
        if mode == "off":
            return True
        from app.lab import features as feat
        b = ctx.bars(13)
        if len(b) < 13 or b["day"].nunique() > 1:
            return True                       # not enough same-day context
        bucket = feat.vwap_slope_bucket(
            float(b.iloc[-1]["vwap"]), float(b.iloc[0]["vwap"]), 12,
            float(b.iloc[-1].get("atr") or 0))
        if mode == "flat_only":
            return bucket == feat.SLOPE_FLAT
        # exclude_strong_against: skip fading a strongly trending VWAP
        if side == self._UP:
            return bucket != feat.SLOPE_STRONG_UP
        return bucket != feat.SLOPE_STRONG_DOWN

    # -- legs and policy ----------------------------------------------------

    def _expiry_rule(self, ctx: BarContext) -> str:
        rule = self.p.get("expiry_rule", "weekly")
        min_dte = int(self.p.get("min_dte", 0))
        if rule == "weekly" and min_dte > 0 and ctx.chain is not None:
            wk = ctx.chain.weekly_expiry()
            if wk:
                from datetime import date as _date
                dte = (_date.fromisoformat(wk)
                       - _date.fromisoformat(ctx.day)).days
                if dte < min_dte:
                    return "monthly"
        return rule

    def _legs(self, ctx: BarContext, side) -> list:
        right = "PE" if side == self._UP else "CE"
        expiry = self._expiry_rule(ctx)
        if self.p["strike_sel"] == "delta":
            spec = LegSpec(side=BUY, right=right, expiry_rule=expiry,
                           strike_rule="DELTA",
                           delta_target=float(self.p["delta_target"]))
        else:
            spec = LegSpec(side=BUY, right=right, expiry_rule=expiry,
                           strike_rule="ATM_OFF",
                           strike_offset=int(self.p["strike_offset"]))
        if self.p["target_mode"] == "vwap_partial" and \
                int(self.p.get("lots", 1)) >= 1:
            return [spec, spec]     # two legs so half can book separately
        return [spec]

    def _policy(self):
        from app.lab.models import ExitPolicy
        tm, sm = self.p["target_mode"], self.p["sl_mode"]
        stop_kind = "pct_value" if sm == "premium_pct" else None
        stop_val = self.p.get("stop_pct") if sm == "premium_pct" else None
        target_kind = target_val = None
        if tm == "premium_pct":
            target_kind, target_val = "pct_value", self.p.get("target_pct")
        elif tm == "risk_reward":
            target_kind = "pct_value"
            target_val = float(self.p["rr_ratio"]) * \
                float(self.p.get("stop_pct") or 30.0)
        trail_kind = trail_val = None
        if tm == "trail_after" and self.p.get("trail_pct"):
            trail_kind, trail_val = "pct_value", self.p.get("trail_pct")
        return ExitPolicy(stop_kind=stop_kind, stop_value=stop_val,
                          target_kind=target_kind, target_value=target_val,
                          trail_kind=trail_kind, trail_value=trail_val)

    def _emit(self, ctx: BarContext, side, mode_letter: str,
              row) -> SignalIntent:
        b = self._bands(row)
        vwap, sd, upper, lower = b
        band = upper if side == self._UP else lower
        direction = "SHORT" if side == self._UP else "LONG"
        tag = "%s_e%d_%s" % ("pe" if side == self._UP else "ce",
                             self._episodes[side], mode_letter)
        note = ("touch ts=%d px=%.2f vwap=%.2f band=%.2f sd=%.2f "
                "dist=%.2f width=%.2f min=%d" % (
                    ctx.ts, float(row["close"]), vwap, band, sd,
                    abs(float(row["close"]) - vwap), (upper - lower),
                    int(row["minute"])))
        return signal(ctx, direction, self._legs(ctx, side),
                      self._policy(), tag=tag, note=note)

    # -- engine interface ---------------------------------------------------

    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        row = ctx.row
        touched = self._track(row)
        b = self._bands(row)
        if b is None:
            return None
        vwap, sd, upper, lower = b
        if int(row["minute"]) < _SESSION_OPEN_MIN + \
                int(self.p["avoid_first_min"]):
            return None

        mode = self.p["entry_mode"]
        if mode == "rejection" and self._pending is not None:
            sig = self._confirm(ctx, row)
            if sig is not None or self._pending is None:
                return sig

        if touched is None:
            return None
        if not self._episode_ok(touched) or not self._slope_ok(ctx, touched):
            return None
        close = float(row["close"])
        if mode == "touch":
            return self._emit(ctx, touched, "A", row)
        if mode == "close_inside":
            inside = close < upper if touched == self._UP else close > lower
            return self._emit(ctx, touched, "B", row) if inside else None
        # rejection: park and wait for confirmation
        self._pending = {"side": touched,
                         "bars_left": int(self.p["confirm_window"]),
                         "touch_hi": float(row["high"]),
                         "touch_lo": float(row["low"])}
        return None

    def _confirm(self, ctx: BarContext, row) -> Optional[SignalIntent]:
        pend = self._pending
        side = pend["side"]
        pend["bars_left"] -= 1
        b = self._bands(row)
        if b is None:
            return None
        _vwap, _sd, upper, lower = b
        close, o = float(row["close"]), float(row["open"])
        hi, lo = float(row["high"]), float(row["low"])
        rule = self.p["confirm_rule"]
        ok = False
        if rule == "close_inside":
            ok = close < upper if side == self._UP else close > lower
        elif rule == "reversal_candle":
            ok = close < o if side == self._UP else close > o
        elif rule == "wick_ratio":
            body = max(abs(close - o), 0.05)
            wick = (hi - max(close, o)) if side == self._UP \
                else (min(close, o) - lo)
            ok = wick / body >= float(self.p["wick_body_min"])
        elif rule == "prev_break":
            ok = close < pend["touch_lo"] if side == self._UP \
                else close > pend["touch_hi"]
        if ok:
            self._pending = None
            if self._episode_ok(side) and self._slope_ok(ctx, side):
                return self._emit(ctx, side, "C", row)
            return None
        if pend["bars_left"] <= 0:
            self._pending = None
        return None

    def wants_exit(self, ctx: BarContext, pos) -> Optional[str]:
        row = ctx.row
        self._track(row)
        b = self._bands(row)
        if b is None:
            return None
        vwap, sd, upper, lower = b
        is_pe = pos.direction == "SHORT"
        tm, sm = self.p["target_mode"], self.p["sl_mode"]

        # v1 target: the underlying tags session VWAP
        if tm in ("vwap", "vwap_partial"):
            reached = float(row["low"]) <= vwap if is_pe \
                else float(row["high"]) >= vwap
            if reached:
                return "vwap_target"
        if sm == "sigma_ext":
            ext = float(self.p["sl_sigma_ext"]) * sd
            blown = float(row["high"]) >= upper + ext if is_pe \
                else float(row["low"]) <= lower - ext
            if blown:
                return "sigma_stop"
        elif sm == "structure":
            beyond = float(row["close"]) > upper if is_pe \
                else float(row["close"]) < lower
            if beyond:
                return "structure_stop"
        elif sm == "time":
            step = 900 if self.p.get("timeframe") == "15m" else 300
            held = (ctx.ts - pos.entry_ts) // step
            if held >= int(self.p["time_stop_bars"]):
                d_entry = abs(pos.spine_entry - vwap)
                d_now = abs(float(row["close"]) - vwap)
                progress = 1.0 - (d_now / d_entry) if d_entry > 0 else 1.0
                if progress * 100.0 < float(self.p["progress_min_pct"]):
                    return "time_stop"
        return None

    def adjust(self, ctx: BarContext, pos) -> Optional[list]:
        if self.p["target_mode"] != "vwap_partial" or len(pos.legs) < 2:
            return None
        if "0" in pos.exit_prices or ctx.chain is None:
            return None
        leg = pos.legs[0]
        q = ctx.chain.quote(leg.expiry, leg.strike, leg.right)
        if q and q["ltp"] >= leg.price * (1 + float(self.p["partial_pct"])
                                          / 100.0):
            return [("close", 0)]
        return None


STRATEGIES = {
    "B01": B01_AtmMomentum, "B02": B02_OrbOption,
    "B03": B03_SupertrendOption, "B04": B04_VwapBreakout,
    "B05": B05_DeltaStrike, "B06": B06_RsiReversal,
    "B07": B07_BreakVolume, "B08": B08_ExpiryDirectional,
    "B09": B09_Vwap3Sigma,
}
