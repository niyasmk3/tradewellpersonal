"""Category F — quantitative strategies (Q01..Q03).

Q02's model is a from-scratch numpy logistic regression, deliberately: no
new dependency on the Py3.9 host, and the learned weights are inspectable
in the trade notes. It refits on a trailing window and NEVER scores a bar
whose label overlapped its training data (an embargo of `horizon_bars`
separates the two).
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from app.lab.engine import BarContext, StrategyBase
from app.lab.models import SignalIntent
from app.lab.strategies.common import (
    fut_leg, fut_policy, nn, opt_buy_leg, prem_policy, signal,
)


class Q01_RegimeRotation(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        r = ctx.row
        if not nn(r.get("atr")):
            return None
        if ctx.regime in ("trend_up", "trend_down"):
            d = "LONG" if ctx.regime == "trend_up" else "SHORT"
            return signal(ctx, d, [opt_buy_leg(d, self.p)],
                          prem_policy(self.p), tag="regime_trend")
        if ctx.regime in ("sideways", "low_vol"):
            day_rng = float(r["day_high"]) - float(r["day_low"])
            if day_rng < 0.8 * float(r["atr"]) * 3:
                return None
            edge = self.p["fade_edge_pct"] / 100.0
            close = float(r["close"])
            hi_edge = float(r["day_low"]) + day_rng * edge
            lo_edge = float(r["day_low"]) + day_rng * (1 - edge)
            if close >= hi_edge:
                return signal(ctx, "SHORT", [opt_buy_leg("SHORT", self.p)],
                              prem_policy(self.p), tag="regime_fade_top")
            if close <= lo_edge:
                return signal(ctx, "LONG", [opt_buy_leg("LONG", self.p)],
                              prem_policy(self.p), tag="regime_fade_bottom")
        return None


_FEATS = ("ret1", "ret3", "ret12", "rsi_c", "vwap_d", "atr_p", "tod", "gap")


def _feature_matrix(b) -> np.ndarray:
    """Causal per-bar features; row i uses bars <= i only."""
    close = b["close"].values
    atr = b["atr"].values
    with np.errstate(divide="ignore", invalid="ignore"):
        ret1 = np.diff(close, prepend=close[0]) / np.where(atr > 0, atr, np.nan)
        ret3 = (close - np.roll(close, 3)) / np.where(atr > 0, atr, np.nan)
        ret12 = (close - np.roll(close, 12)) / np.where(atr > 0, atr, np.nan)
    ret3[:3] = np.nan
    ret12[:12] = np.nan
    rsi_c = b["rsi"].values / 100.0 - 0.5
    vwap_d = (close - b["vwap"].values) / np.where(atr > 0, atr, np.nan)
    atr_p = b["atr_pctile"].values / 100.0 - 0.5
    tod = (b["minute"].values - 555) / 375.0 - 0.5
    gap = np.clip(b["gap_pct"].values, -2, 2) / 2.0
    return np.column_stack([ret1, ret3, ret12, rsi_c, vwap_d, atr_p, tod,
                            gap])


def _fit_logistic(x: np.ndarray, y: np.ndarray, iters: int = 300,
                  lr: float = 0.1, l2: float = 1e-3) -> np.ndarray:
    """Plain gradient descent with L2; returns weights incl. bias."""
    xb = np.column_stack([np.ones(len(x)), x])
    w = np.zeros(xb.shape[1])
    for _ in range(iters):
        z = xb @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        grad = xb.T @ (p - y) / len(y) + l2 * w
        w -= lr * grad
    return w


class Q02_MlLogistic(StrategyBase):
    def __init__(self, sdef, params):
        super().__init__(sdef, params)
        self._w: Optional[np.ndarray] = None
        self._mu: Optional[np.ndarray] = None
        self._sd: Optional[np.ndarray] = None
        self._fit_day: Optional[str] = None
        self._days_since_fit = 999

    def day_reset(self) -> None:
        self._days_since_fit += 1

    def _maybe_fit(self, ctx: BarContext) -> None:
        if self._w is not None and \
                self._days_since_fit < int(self.p["refit_days"]):
            return
        horizon = int(self.p["horizon_bars"])
        train_bars = int(self.p["train_days"]) * 75
        b = ctx.bars(train_bars + horizon + 1)
        # embargo: usable training rows need their forward label realised
        # strictly before the current bar.
        if len(b) < train_bars // 2:
            return
        train = b.iloc[:-(horizon + 1)]
        if len(train) < 500:
            return
        x = _feature_matrix(train)
        close = train["close"].values
        fwd = np.roll(close, -horizon) - close
        fwd[-horizon:] = np.nan
        y = (fwd > 0).astype(float)
        ok = ~(np.isnan(x).any(axis=1) | np.isnan(fwd))
        x, y = x[ok], y[ok]
        if len(y) < 500 or y.std() == 0:
            return
        self._mu, self._sd = x.mean(axis=0), x.std(axis=0)
        self._sd[self._sd == 0] = 1.0
        self._w = _fit_logistic((x - self._mu) / self._sd, y)
        self._fit_day = ctx.day
        self._days_since_fit = 0

    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        self._maybe_fit(ctx)
        if self._w is None:
            return None
        b = ctx.bars(20)
        if len(b) < 20:
            return None
        x = _feature_matrix(b)[-1]
        if np.isnan(x).any():
            return None
        z = np.concatenate([[1.0], (x - self._mu) / self._sd]) @ self._w
        prob = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        band = float(self.p["band"])
        note = "p_up=%.3f fit=%s" % (prob, self._fit_day)
        if prob >= 0.5 + band:
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="ml_long", note=note)
        if prob <= 0.5 - band:
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="ml_short", note=note)
        return None


def _votes(row) -> Optional[int]:
    if not nn(row.get("ema_fast"), row.get("ema_slow"), row.get("vwap"),
              row.get("rsi"), row.get("prev_high"), row.get("prev_low")):
        return None
    v = 0
    v += 1 if row["ema_fast"] > row["ema_slow"] else -1
    v += 1 if row["st_dir"] > 0 else (-1 if row["st_dir"] < 0 else 0)
    v += 1 if row["close"] > row["vwap"] else -1
    v += 1 if row["rsi"] > 55 else (-1 if row["rsi"] < 45 else 0)
    v += 1 if row["close"] > row["prev_high"] else (
        -1 if row["close"] < row["prev_low"] else 0)
    return v


class Q03_Ensemble(StrategyBase):
    def on_bar(self, ctx: BarContext) -> Optional[SignalIntent]:
        v = _votes(ctx.row)
        if v is None:
            return None
        need = int(self.p["min_votes"])
        if v >= need:
            return signal(ctx, "LONG", [fut_leg("LONG")], fut_policy(self.p),
                          tag="ensemble_%d" % v)
        if v <= -need:
            return signal(ctx, "SHORT", [fut_leg("SHORT")],
                          fut_policy(self.p), tag="ensemble_%d" % v)
        return None

    def wants_exit(self, ctx, pos) -> Optional[str]:
        v = _votes(ctx.row)
        if v is None:
            return None
        keep = int(self.p["exit_votes"])
        if pos.direction == "LONG" and v < keep:
            return "votes_faded"
        if pos.direction == "SHORT" and v > -keep:
            return "votes_faded"
        return None


STRATEGIES = {
    "Q01": Q01_RegimeRotation, "Q02": Q02_MlLogistic, "Q03": Q03_Ensemble,
}
