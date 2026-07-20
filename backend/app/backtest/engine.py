"""Directional backtest engine.

Replays the regime classifier + technical score over historical candles and
grades each signal on the underlying: from the (delayed) entry, did price reach
the target-implied level or the invalidation level first? Reuses the live
scoring component functions and regime classifier so the backtested logic
matches production (minus the live-only OI/news inputs, which aren't in history).

Windows are scoped to the current **session day** — exactly as the live engine
builds candles — so VWAP/EMA/Supertrend reset each day and match live behaviour
(a multi-day rolling window would compute a bogus cross-session VWAP).
"""
from __future__ import annotations

import pandas as pd

from app.market.indicators import compute_snapshot
from app.signals import regime as regime_mod
from app.signals import scoring
from app.signals.models import Bias, Direction
from app.signals.modes import ModeProfile
from app.state import _vix_status
from app.backtest.models import BacktestResult, BacktestTrade

_IST_OFFSET = 19800
_ENTRY_DELAY = 1       # enter on the next bar's open (models reaction lag)
_MIN_RISK_MULT = 2.0   # skip trades whose risk barely exceeds round-trip slippage
# Technical components (OI/news are live-only, excluded): price 25 + trend 20 +
# volume 15 + volatility 10 = 70. With historical VIX threaded in, volatility can
# reach its full 10, so 70 is the true achievable max and the rescale is exact.
_TECH_MAX = 70.0


def _day(ts: int) -> int:
    return (ts + _IST_OFFSET) // 86400


def _technical_score(window: pd.DataFrame, ind, bullish: bool, dh, dl, vix_status) -> float:
    pts = (
        scoring._price_action(window, ind, bullish, dh, dl).points
        + scoring._trend(ind, bullish).points
        + scoring._volume(window, bullish).points
        + scoring._volatility(ind, vix_status).points
    )
    return round(pts / _TECH_MAX * 100, 1)


def _day_context(days: list[int], c, vix_by_day: dict | None):
    """Per-session context from PRIOR sessions only (no look-ahead):
    {day -> prior day's last close} and {day -> prior day's VIX status}."""
    distinct: list[int] = []
    last_close: dict[int, float] = {}
    for idx, d in enumerate(days):
        if not distinct or distinct[-1] != d:
            distinct.append(d)
        last_close[d] = float(c[idx])          # ends as the day's final close
    prev_close_by_day: dict = {}
    vix_status_by_day: dict = {}
    for i, d in enumerate(distinct):
        prev = distinct[i - 1] if i > 0 else None
        prev_close_by_day[d] = last_close.get(prev) if prev is not None else None
        vclose = vix_by_day.get(prev) if (vix_by_day and prev is not None) else None
        vix_status_by_day[d] = _vix_status(vclose)   # _vix_status(None) -> None
    return prev_close_by_day, vix_status_by_day


def run(df: pd.DataFrame, profile: ModeProfile, symbol: str,
        slippage_pts: float = 1.5, max_hold_bars: int = 80,
        vix_by_day: dict | None = None) -> BacktestResult:
    # Defensive: every downstream assumption (day grouping, prior-day context,
    # bar play-out) requires ascending time. fetch_futures already sorts, but a
    # future caller might not — an unsorted frame would silently create look-ahead.
    if len(df) and not df["ts"].is_monotonic_increasing:
        df = df.sort_values("ts").reset_index(drop=True)
    slippage_pts = max(0.0, slippage_pts)  # negative slippage would break the risk floor
    n = len(df)
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    low = df["low"].to_numpy()
    c = df["close"].to_numpy()
    ts = df["ts"].to_numpy()
    days = [_day(int(t)) for t in ts]

    warmup = profile.min_candles
    threshold = profile.score_valid
    rr = profile.rr_target1
    intraday = profile.mode.value == "intraday"
    min_risk = _MIN_RISK_MULT * slippage_pts

    # Per-day context that matches live inputs, using only PRIOR sessions (no
    # look-ahead): prev-day close (day-direction vote) and prior-day VIX status.
    prev_close_by_day, vix_status_by_day = _day_context(days, c, vix_by_day)

    trades: list[BacktestTrade] = []
    open_t: dict | None = None
    day_hi = day_lo = None
    cur_day = None
    day_start = 0
    cur_prev_close = None
    cur_vix = None

    for i in range(n):
        # session bookkeeping — reset window origin + running extremes each day
        if days[i] != cur_day:
            cur_day, day_hi, day_lo, day_start = days[i], h[i], low[i], i
            cur_prev_close = prev_close_by_day.get(days[i])
            cur_vix = vix_status_by_day.get(days[i])
        else:
            day_hi, day_lo = max(day_hi, h[i]), min(day_lo, low[i])

        # --- manage an open position ---
        if open_t is not None:
            if i >= open_t["entry_idx"]:
                ce = open_t["direction"] is Direction.CE
                exit_px = None
                outcome = None
                if ce:
                    if low[i] <= open_t["stop"]:      # conservative: stop before target
                        exit_px, outcome = open_t["stop"], "stop"
                    elif h[i] >= open_t["target"]:
                        exit_px, outcome = open_t["target"], "target"
                else:
                    if h[i] >= open_t["stop"]:
                        exit_px, outcome = open_t["stop"], "stop"
                    elif low[i] <= open_t["target"]:
                        exit_px, outcome = open_t["target"], "target"

                if exit_px is None:
                    is_last = i == n - 1
                    day_end = intraday and (is_last or days[i + 1] != days[i])
                    timed_out = (not intraday) and (i - open_t["entry_idx"]) >= max_hold_bars
                    if is_last or day_end or timed_out:
                        exit_px = c[i]
                        outcome = "eod" if intraday else "time"

                if exit_px is not None:
                    fill = exit_px - slippage_pts if ce else exit_px + slippage_pts  # exit slippage against us
                    r = (fill - open_t["entry"]) / open_t["risk"] if ce else (open_t["entry"] - fill) / open_t["risk"]
                    trades.append(BacktestTrade(
                        entry_ts=int(open_t["entry_ts"]), exit_ts=int(ts[i]),
                        direction=open_t["direction"], regime=open_t["regime"], score=open_t["score"],
                        entry=round(open_t["entry"], 2), stop=round(open_t["stop"], 2),
                        target=round(open_t["target"], 2), exit=round(fill, 2),
                        r_multiple=round(r, 3), outcome=outcome,
                    ))
                    open_t = None
            continue  # one position at a time — no new signal while in a trade

        # --- flat: evaluate a signal as-of bar i, using this session's bars only ---
        if (i - day_start + 1) < warmup:
            continue
        window = df.iloc[day_start: i + 1]
        ind = compute_snapshot(window)
        reg = regime_mod.classify(window, ind, prev_close=cur_prev_close,
                                  vix_status=cur_vix, min_candles=warmup)
        if not reg.tradeable:
            continue
        bullish = reg.bias is Bias.BULLISH
        score = _technical_score(window, ind, bullish, day_hi, day_lo, cur_vix)
        if score < threshold:
            continue

        entry_idx = i + _ENTRY_DELAY
        if entry_idx >= n or days[entry_idx] != days[i]:
            continue  # no same-session bar to enter on (don't fill across an overnight gap)
        entry_raw = o[entry_idx]
        entry = entry_raw + slippage_pts if bullish else entry_raw - slippage_pts
        tail = window.tail(3)
        if bullish:
            stop = float(tail["low"].min())
            risk = entry - stop
            if risk <= min_risk:
                continue  # degenerate/near-zero risk — dominated by costs, skip
            target = entry + rr * risk
        else:
            stop = float(tail["high"].max())
            risk = stop - entry
            if risk <= min_risk:
                continue
            target = entry - rr * risk

        open_t = {
            "entry_idx": entry_idx, "direction": Direction.CE if bullish else Direction.PE,
            "entry": entry, "stop": stop, "target": target, "risk": risk,
            "regime": reg.regime.value, "score": score, "entry_ts": ts[entry_idx],
        }

    return _summarize(df, profile, symbol, trades)


def _summarize(df, profile, symbol, trades: list[BacktestTrade]) -> BacktestResult:
    n = len(df)
    rs = [t.r_multiple for t in trades]
    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple <= 0]
    ce = [t for t in trades if t.direction is Direction.CE]
    pe = [t for t in trades if t.direction is Direction.PE]

    def wr(group):
        return round(100 * sum(1 for t in group if t.r_multiple > 0) / len(group), 1) if group else 0.0

    equity: list[float] = []
    cum = peak = 0.0
    max_dd = 0.0
    for r in rs:
        cum = round(cum + r, 3)
        equity.append(cum)
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    sum_win = sum(t.r_multiple for t in wins)
    sum_loss = sum(t.r_multiple for t in losses)
    note = (
        "Directional backtest on the underlying (near-month future). Grades the "
        "signal thesis, not exact option P&L — theta and premium fills are excluded. "
        "Scoring uses price action, trend, volume, volatility (with historical VIX) "
        "and regime; the live-only option-chain OI and news components are absent "
        "from history, so the pass gate is a technical-strength threshold and the "
        "selected trades differ from the live engine's."
    )
    return BacktestResult(
        symbol=symbol, mode=profile.mode, timeframe=profile.timeframe,
        from_ts=int(df["ts"].iloc[0]) if n else 0, to_ts=int(df["ts"].iloc[-1]) if n else 0,
        bars=n, trades_total=len(trades), wins=len(wins), losses=len(losses),
        win_rate=round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        expectancy_r=round(sum(rs) / len(rs), 3) if rs else 0.0,
        avg_win_r=round(sum_win / len(wins), 3) if wins else 0.0,
        avg_loss_r=round(sum_loss / len(losses), 3) if losses else 0.0,
        profit_factor=round(sum_win / abs(sum_loss), 2) if sum_loss < 0 else None,
        max_drawdown_r=round(max_dd, 3), total_r=round(sum(rs), 3),
        ce_trades=len(ce), ce_win_rate=wr(ce), pe_trades=len(pe), pe_win_rate=wr(pe),
        equity_curve=equity, trades=trades[-200:], note=note,
    )
