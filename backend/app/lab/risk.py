"""Shared portfolio risk engine for the Lab's paper trading.

Independent of any strategy: every simulated order passes through
`check_order` and every session tick through `check_session`. Limits are
runtime-configurable (risk_config table) but each has a hard code-side
bound so no UI mishap can set a nonsense cap. The emergency stop is a
persisted latch: tripping it blocks new paper orders immediately; the
configured policy decides whether existing positions are frozen (managed
to their normal exits) or flattened at the next tick.

Everything here concerns SIMULATED positions. There is no order path.
"""
from __future__ import annotations

import time
from typing import Optional

from app.lab.store import LabStore, lab_store

# key -> (default, lo, hi)
LIMITS = {
    "max_capital_rs":            (500000.0, 10000.0, 100000000.0),
    "max_alloc_per_strategy_rs": (200000.0, 5000.0, 100000000.0),
    "max_open_positions":        (3.0, 1.0, 20.0),
    "max_lots_per_order":        (2.0, 1.0, 50.0),
    "max_daily_loss_rs":         (10000.0, 500.0, 10000000.0),
    "max_total_drawdown_rs":     (30000.0, 1000.0, 10000000.0),
    "max_margin_util_pct":       (60.0, 5.0, 100.0),
    "max_expiry_day_positions":  (1.0, 0.0, 10.0),
    "max_short_premium_positions": (1.0, 0.0, 10.0),
    "emergency_stop":            (0.0, 0.0, 1.0),
    "emergency_flatten":         (0.0, 0.0, 1.0),
}


def effective(store: LabStore = lab_store) -> dict:
    cfg = store.risk_config()
    out = {}
    for key, (default, lo, hi) in LIMITS.items():
        v = cfg.get(key, default)
        out[key] = min(max(float(v), lo), hi)
    return out


def set_limits(updates: dict, store: LabStore = lab_store) -> dict:
    clean = {}
    for k, v in updates.items():
        if k not in LIMITS:
            raise ValueError("unknown risk key: %s" % k)
        default, lo, hi = LIMITS[k]
        clean[k] = min(max(float(v), lo), hi)
    store.set_risk_config(clean)
    return effective(store)


class RiskDecision:
    def __init__(self, allowed: bool, code: str = "", detail: str = ""):
        self.allowed = allowed
        self.code = code
        self.detail = detail


def _day_pnl(store: LabStore, day: str) -> float:
    total = 0.0
    for p in store.positions_for_day(day):
        if p.get("pnl_net") is not None:
            total += p["pnl_net"]
    return total


def _cum_pnl(store: LabStore) -> float:
    total = 0.0
    for d in store.paper_days(limit=10000):
        total += d["pnl_net"]
    return total


def check_order(sid: str, day: str, legs_est: list, margin_est: float,
                expiry_day: bool, lots: int,
                store: LabStore = lab_store, version: Optional[int] = None,
                experimental: Optional[bool] = None) -> RiskDecision:
    """Gate one prospective paper order. legs_est: dicts with side/right.
    version/experimental identify the ARM placing the order: a champion and
    its experimental challenger are separate research portfolios (spec §9)
    and may hold positions in parallel — the one-position rule applies per
    arm, not per sid. Fails closed on its own bugs — a broken risk engine
    must not trade."""
    try:
        cfg = effective(store)
        if cfg["emergency_stop"] >= 1:
            return RiskDecision(False, "emergency_stop", "latch is tripped")
        open_pos = store.open_positions()
        if len(open_pos) >= int(cfg["max_open_positions"]):
            return RiskDecision(False, "max_open_positions",
                                "%d open" % len(open_pos))
        if lots > int(cfg["max_lots_per_order"]):
            return RiskDecision(False, "max_lots", "%d lots" % lots)
        day_pnl = _day_pnl(store, day)
        if day_pnl <= -cfg["max_daily_loss_rs"]:
            return RiskDecision(False, "daily_loss_stop",
                                "day pnl %.0f" % day_pnl)
        cum = _cum_pnl(store) + day_pnl
        # drawdown vs high-water of the cumulative paper ledger
        hw = 0.0
        run = 0.0
        for d in reversed(store.paper_days(limit=10000)):
            run += d["pnl_net"]
            hw = max(hw, run)
        if (hw - (cum)) >= cfg["max_total_drawdown_rs"]:
            return RiskDecision(False, "max_drawdown",
                                "dd %.0f" % (hw - cum))
        if margin_est > cfg["max_capital_rs"] * \
                cfg["max_margin_util_pct"] / 100.0:
            return RiskDecision(False, "margin_cap",
                                "est margin %.0f" % margin_est)
        # one live position per ARM (sid + version + experimental flag);
        # champion and challenger are deliberately separate portfolios
        arm_open = [p for p in open_pos if p["sid"] == sid
                    and (version is None or p["version"] == version)
                    and (experimental is None
                         or bool(p["experimental"]) == experimental)]
        if arm_open:
            return RiskDecision(False, "arm_position_exists",
                                "one live position per strategy arm")
        if margin_est > cfg["max_alloc_per_strategy_rs"]:
            return RiskDecision(False, "strategy_alloc",
                                "est margin %.0f" % margin_est)
        has_short = any(l.get("side") == "SELL" and l.get("right") != "FUT"
                        for l in legs_est)
        if has_short:
            n_short = sum(1 for p in open_pos if any(
                l.get("side") == "SELL" and l.get("right") != "FUT"
                for l in p["legs"]))
            if n_short >= int(cfg["max_short_premium_positions"]):
                return RiskDecision(False, "short_premium_cap",
                                    "%d short-premium open" % n_short)
        if expiry_day:
            n_exp = sum(1 for p in open_pos if p["day"] == day)
            if n_exp >= int(cfg["max_expiry_day_positions"]):
                return RiskDecision(False, "expiry_day_cap",
                                    "%d positions today" % n_exp)
        return RiskDecision(True)
    except Exception as exc:
        return RiskDecision(False, "risk_engine_error", str(exc))


def trip_emergency(reason: str, flatten: bool,
                   store: LabStore = lab_store) -> None:
    store.set_risk_config({"emergency_stop": 1.0,
                           "emergency_flatten": 1.0 if flatten else 0.0})
    store.log_risk_event(int(time.time()), "", "", "emergency_stop",
                         {"reason": reason, "flatten": flatten})


def clear_emergency(store: LabStore = lab_store) -> None:
    store.set_risk_config({"emergency_stop": 0.0, "emergency_flatten": 0.0})
    store.log_risk_event(int(time.time()), "", "", "emergency_clear", {})
