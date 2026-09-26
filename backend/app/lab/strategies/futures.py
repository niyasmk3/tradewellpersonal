"""Category A — NIFTY futures strategies (F01..F10).

Each on_bar sees the decision-bar features and returns a SignalIntent or
None. The engine owns windows, trade caps, regime/VIX filters and fills.
"""
from __future__ import annotations

from typing import Optional

from app.lab.engine import BarContext, StrategyBase
from app.lab.models import ExitPolicy, SignalIntent
from app.lab.strategies.common import (
    OncePerDayMixin, fut_leg, fut_policy, nn, signal,
)

_SESSION_OPEN_MIN = 555


class F01_ORB(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        r = ctx.row
        if not nn(r.get("atr")):
            return None
        orb_end = _SESSION_OPEN_MIN + int(self.p["orb_minutes"])
        if int(r["minute"]) < orb_end:            # range still forming
            return None
        day_bars = ctx.bars(90)
        day_bars = day_bars[day_bars["day"] == r["day"]]
        rng = day_bars[day_bars["minute"] < orb_end]
        if rng.empty:
            return None
        orb_hi, orb_lo = float(rng["high"].max()), float(rng["low"].min())
        close, atr = float(r["close"]), float(r["atr"])
        if close > orb_hi and not self.fired("long"):
            self.mark("long")
            stop_pts = min(close - orb_lo, self.p["stop_atr"] * atr)
            pol = ExitPolicy(stop_kind="points_spine", stop_value=stop_pts,
                             target_kind="atr_spine",
                             target_value=self.p["target_atr"],
                             trail_kind="risk_pct" if self.p.get("trail_pct")
                             else None,
                             trail_value=self.p.get("trail_pct") or None)
            return signal(ctx, "LONG", [fut_leg("LONG")], pol, tag="orb_up")
        if close < orb_lo and not self.fired("short"):
            self.mark("short")
            stop_pts = min(orb_hi - close, self.p["stop_atr"] * atr)
            pol = ExitPolicy(stop_kind="points_spine", stop_value=stop_pts,
                             target_kind="atr_spine",
                             target_value=self.p["target_atr"],
                             trail_kind="risk_pct" if self.p.get("trail_pct")
                             else None,
                             trail_value=self.p.get("trail_pct") or None)
            return signal(ctx, "SHORT", [fut_leg("SHORT")], pol, tag="orb_dn")
        return None


class F02_EmaCross(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        b = ctx.bars(2)
        if len(b) < 2:
            return None
        prev, cur = b.iloc[0], b.iloc[1]
        if not nn(prev["ema_fast"], prev["ema_slow"], cur["ema_fast"],
                  cur["ema_slow"], cur["adx"], cur["atr"]):
            return None
        if cur["adx"] < self.p["adx_min"]:
            return None
        up = prev["ema_fast"] <= prev["ema_slow"] and \
            cur["ema_fast"] > cur["ema_slow"]
        dn = prev["ema_fast"] >= prev["ema_slow"] and \
            cur["ema_fast"] < cur["ema_slow"]
        if up:
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="golden_cross")
        if dn:
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="death_cross")
        return None

    def wants_exit(self, ctx, pos) -> Optional[str]:
        r = ctx.row
        if not nn(r.get("ema_fast"), r.get("ema_slow")):
            return None
        if pos.direction == "LONG" and r["ema_fast"] < r["ema_slow"]:
            return "reverse_cross"
        if pos.direction == "SHORT" and r["ema_fast"] > r["ema_slow"]:
            return "reverse_cross"
        return None


class F03_Supertrend(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        b = ctx.bars(2)
        if len(b) < 2:
            return None
        prev, cur = b.iloc[0], b.iloc[1]
        if not nn(cur["adx"], cur["atr"]) or cur["adx"] < self.p["adx_min"]:
            return None
        if prev["st_dir"] <= 0 and cur["st_dir"] > 0:
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="st_flip_up")
        if prev["st_dir"] >= 0 and cur["st_dir"] < 0:
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="st_flip_dn")
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


class F04_VwapTrend(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        n = int(self.p["hold_bars"])
        b = ctx.bars(n + 1)
        if len(b) < n + 1:
            return None
        cur = b.iloc[-1]
        if not nn(cur["vwap"], cur["ema20"], b.iloc[-2]["ema20"],
                  cur["atr"]):
            return None
        window = b.iloc[-n:]
        # every close in the window must sit on one side AND stay same day
        if window["day"].nunique() > 1:
            return None
        above = (window["close"] > window["vwap"]).all()
        below = (window["close"] < window["vwap"]).all()
        ema_up = cur["ema20"] > b.iloc[-2]["ema20"]
        if above and ema_up and not self.fired("long"):
            self.mark("long")
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="vwap_hold_up")
        if below and not ema_up and not self.fired("short"):
            self.mark("short")
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="vwap_hold_dn")
        return None

    def wants_exit(self, ctx, pos) -> Optional[str]:
        r = ctx.row
        if not nn(r.get("vwap")):
            return None
        if pos.direction == "LONG" and r["close"] < r["vwap"]:
            return "vwap_cross"
        if pos.direction == "SHORT" and r["close"] > r["vwap"]:
            return "vwap_cross"
        return None


class F05_PrevDayBreak(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        r = ctx.row
        if not nn(r.get("prev_high"), r.get("prev_low"), r.get("atr")):
            return None
        if r["close"] > r["prev_high"] and not self.fired("long"):
            self.mark("long")
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="pdh_break")
        if r["close"] < r["prev_low"] and not self.fired("short"):
            self.mark("short")
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="pdl_break")
        return None


class F06_GapMomentum(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        r = ctx.row
        if not nn(r.get("gap_pct"), r.get("prev_close"), r.get("atr")):
            return None
        gap = float(r["gap_pct"])
        if abs(gap) < self.p["gap_min_pct"] or self.fired("done"):
            return None
        bars_in_day = int((r["minute"] - _SESSION_OPEN_MIN) // 5) + 1
        if bars_in_day < int(self.p["confirm_bars"]) or bars_in_day > 24:
            return None
        mid = (float(r["prev_close"]) + float(r["day_open"])) / 2.0
        close = float(r["close"])
        if gap > 0 and close > mid:
            self.mark("done")
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="gap_up_go")
        if gap < 0 and close < mid:
            self.mark("done")
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="gap_dn_go")
        return None


class F07_GapFill(OncePerDayMixin, StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        r = ctx.row
        if not nn(r.get("gap_pct"), r.get("prev_close"), r.get("atr")):
            return None
        gap = float(r["gap_pct"])
        if not (self.p["gap_min_pct"] <= abs(gap) <= self.p["gap_max_pct"]):
            return None
        if self.fired("done"):
            return None
        bars_in_day = int((r["minute"] - _SESSION_OPEN_MIN) // 5) + 1
        if bars_in_day < int(self.p["confirm_bars"]) or bars_in_day > 24:
            return None
        close, atr = float(r["close"]), float(r["atr"])
        fill_pts_cap = self.p["target_atr"] * atr
        if gap > 0 and close < float(r["day_open"]):     # up-gap failing
            self.mark("done")
            tgt = min(close - float(r["prev_close"]), fill_pts_cap)
            if tgt <= 0:
                return None
            pol = ExitPolicy(stop_kind="atr_spine",
                             stop_value=self.p["stop_atr"],
                             target_kind="points_spine", target_value=tgt)
            return signal(ctx, "SHORT", [fut_leg("SHORT")], pol,
                          tag="gap_fill_dn")
        if gap < 0 and close > float(r["day_open"]):     # down-gap failing
            self.mark("done")
            tgt = min(float(r["prev_close"]) - close, fill_pts_cap)
            if tgt <= 0:
                return None
            pol = ExitPolicy(stop_kind="atr_spine",
                             stop_value=self.p["stop_atr"],
                             target_kind="points_spine", target_value=tgt)
            return signal(ctx, "LONG", [fut_leg("LONG")], pol,
                          tag="gap_fill_up")
        return None


class F08_RsiVwapRevert(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        r = ctx.row
        if not nn(r.get("rsi"), r.get("vwap"), r.get("atr")):
            return None
        close, vwap, atr = float(r["close"]), float(r["vwap"]), float(r["atr"])
        dist = self.p["dist_atr"] * atr
        if r["rsi"] < self.p["rsi_low"] and close < vwap - dist:
            pol = ExitPolicy(stop_kind="atr_spine",
                             stop_value=self.p["stop_atr"],
                             target_kind="points_spine",
                             target_value=max(vwap - close, 1.0))
            return signal(ctx, "LONG", [fut_leg("LONG")], pol,
                          tag="revert_up")
        if r["rsi"] > self.p["rsi_high"] and close > vwap + dist:
            pol = ExitPolicy(stop_kind="atr_spine",
                             stop_value=self.p["stop_atr"],
                             target_kind="points_spine",
                             target_value=max(close - vwap, 1.0))
            return signal(ctx, "SHORT", [fut_leg("SHORT")], pol,
                          tag="revert_dn")
        return None


class F09_BollingerSqueeze(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        look = int(self.p["squeeze_lookback"])
        b = ctx.bars(max(look, 21))
        if len(b) < max(look, 21):
            return None
        r = b.iloc[-1]
        if not nn(r["bb_width"], r["atr"]):
            return None
        widths = b["bb_width"].dropna().tail(look)
        if len(widths) < look // 2:
            return None
        pctile = (widths <= float(r["bb_width"])).mean() * 100.0
        if pctile > self.p["squeeze_pctile"]:
            return None
        closes = b["close"].tail(20)
        mean, sd = closes.mean(), closes.std()
        if not sd or sd != sd:
            return None
        upper, lower = mean + 2 * sd, mean - 2 * sd
        close = float(r["close"])
        if close > upper:
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="squeeze_break_up")
        if close < lower:
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="squeeze_break_dn")
        return None


class F10_PriceOI(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        look = int(self.p["look_bars"])
        b = ctx.bars(look + 1)
        if len(b) < look + 1 or "oi" not in b.columns:
            return None
        r = b.iloc[-1]
        if not nn(r.get("atr")):
            return None
        oi_then = float(b.iloc[0]["oi"] or 0)
        oi_now = float(r["oi"] or 0)
        if oi_then <= 0 or oi_now <= 0:
            return None
        px_move = float(r["close"]) - float(b.iloc[0]["close"])
        oi_chg_pct = (oi_now - oi_then) / oi_then * 100.0
        if oi_chg_pct < self.p["oi_min_pct"]:
            return None
        need = self.p["px_atr"] * float(r["atr"])
        if px_move >= need:
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="long_buildup")
        if px_move <= -need:
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="short_buildup")
        return None


STRATEGIES = {
    "F01": F01_ORB, "F02": F02_EmaCross, "F03": F03_Supertrend,
    "F04": F04_VwapTrend, "F05": F05_PrevDayBreak, "F06": F06_GapMomentum,
    "F07": F07_GapFill, "F08": F08_RsiVwapRevert,
    "F09": F09_BollingerSqueeze, "F10": F10_PriceOI,
}
