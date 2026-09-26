"""Performance statistics for backtest runs and paper ledgers.

WIN RATE IS NOT PROFITABILITY: every summary carries expectancy, profit
factor, drawdown and observation counts next to the win rate, and a
`warnings` list that flags any figure resting on too few trades. Nothing
here extrapolates: a bucket with 3 trades reports 3 trades.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from app.market.calendar import IST

MIN_TRADES_RELIABLE = 30
MIN_DAYS_SHARPE = 20


def _dd(curve: list) -> float:
    peak, worst = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        worst = min(worst, v - peak)
    return worst


def _streak(pnls: list) -> int:
    worst = cur = 0
    for p in pnls:
        cur = cur + 1 if p < 0 else 0
        worst = max(worst, cur)
    return worst


def _bucket(trades: list, key_fn) -> dict:
    out: dict = {}
    for t in trades:
        k = str(key_fn(t) if key_fn(t) is not None else "unknown")
        b = out.setdefault(k, {"n": 0, "net": 0.0, "wins": 0})
        b["n"] += 1
        b["net"] += t["pnl_net"]
        if t["pnl_net"] > 0:
            b["wins"] += 1
    for b in out.values():
        b["net"] = round(b["net"], 2)
        b["win_rate"] = round(b["wins"] / b["n"] * 100, 1) if b["n"] else None
        b["expectancy"] = round(b["net"] / b["n"], 2) if b["n"] else None
        b["low_sample"] = b["n"] < MIN_TRADES_RELIABLE
    return out


def summarize(trades: list, capital_rs: float, daily_pnl: dict,
              rejects: dict, equity_curve: list, kind: str,
              peak_margin: float = 0.0, bars: int = 0,
              step_s: int = 300) -> dict:
    """kind: 'insample' | 'oos' | 'walkforward' | 'paper' — carried on the
    result so the UI can never blend evidence classes into one number."""
    n = len(trades)
    pnls = [t["pnl_net"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    net = sum(pnls)
    warnings = []
    if n == 0:
        warnings.append("no trades in this run — nothing can be concluded")
    elif n < MIN_TRADES_RELIABLE:
        warnings.append("only %d trades — treat every figure as anecdote, "
                        "not evidence (need %d+)" % (n, MIN_TRADES_RELIABLE))

    daily = sorted(daily_pnl.items())
    sharpe = None
    if len(daily) >= MIN_DAYS_SHARPE:
        vals = [v for _, v in daily]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        sd = math.sqrt(var)
        if sd > 0:
            sharpe = round(mean / sd * math.sqrt(252), 2)
    else:
        warnings.append("fewer than %d trading days — Sharpe withheld"
                        % MIN_DAYS_SHARPE)

    time_in_market = None
    if bars > 0:
        held = sum(t.get("bars_held", 0) for t in trades)
        time_in_market = round(held / bars * 100, 1)

    by_month: dict = {}
    for day, v in daily:
        m = day[:7]
        by_month[m] = round(by_month.get(m, 0.0) + v, 2)

    out = {
        "kind": kind,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy": round(net / n, 2) if n else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(net, 2),
        "profit_factor": round(gross_profit / gross_loss, 2)
                         if gross_loss > 0 else None,
        "risk_reward": round((sum(wins) / len(wins)) /
                             abs(sum(losses) / len(losses)), 2)
                       if wins and losses and sum(losses) != 0 else None,
        "max_drawdown": round(_dd(equity_curve), 2),
        "longest_loss_streak": _streak(pnls),
        "capital_rs": capital_rs,
        "return_on_capital_pct": round(net / capital_rs * 100, 2)
                                 if capital_rs else None,
        "peak_margin_rs": round(peak_margin, 0),
        "margin_utilization_pct": round(peak_margin / capital_rs * 100, 1)
                                  if capital_rs else None,
        "margin_note": "margin figures are ESTIMATES (no SPAN feed)",
        "sharpe": sharpe,
        "time_in_market_pct": time_in_market,
        "daily_pnl": [{"day": d, "net": round(v, 2)} for d, v in daily],
        "monthly_pnl": by_month,
        "by_session": _bucket(trades, lambda t: t.get("session_bucket")),
        "by_expiry_day": _bucket(
            trades, lambda t: "expiry" if t.get("expiry_day") else "normal"),
        "by_regime": _bucket(trades, lambda t: t.get("regime") or None),
        "by_exit_reason": _bucket(trades, lambda t: t.get("exit_reason")),
        "by_direction": _bucket(trades, lambda t: t.get("direction")),
        "rejected_signals": dict(sorted(rejects.items(),
                                        key=lambda kv: -kv[1])),
        "warnings": warnings,
    }
    return out


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, IST).date().isoformat()


def merge_daily(trades: list) -> dict:
    out: dict = {}
    for t in trades:
        d = day_of(t["exit_ts"])
        out[d] = out.get(d, 0.0) + t["pnl_net"]
    return out
