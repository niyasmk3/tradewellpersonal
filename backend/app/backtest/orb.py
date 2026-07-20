"""Opening Range Breakout (ORB) — a prototype strategy for the market open.

WHY THIS EXISTS, separately from the signal engine: the live engine is built on
lagging trend confirmation (EMA alignment, ADX, structure, Supertrend), which
needs bars to accumulate and therefore cannot speak before ~10:00. Measured
09:15-12:30 expectancy on the fastest reachable config was negative in every
30-minute bucket. ORB inverts the premise — the opening range IS the signal, so
no warm-up is required.

Rules (one position at a time, intraday only):
  * Opening range = high/low of the first `or_minutes` of the session.
  * A CLOSE beyond the range arms an entry; the fill is the NEXT bar's open
    (never the breakout bar itself — that would be look-ahead).
  * Stop = opposite side of the range. Target = entry ± rr x risk.
  * Same-bar stop+target => stop assumed first (conservative).
  * Everything is force-closed at the session's last bar.

Grades the UNDERLYING future, exactly like backtest/engine.py — so theta, IV
and premium spreads are excluded and real option results would be worse.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.backtest.models import BacktestResult, BacktestTrade
from app.signals.models import Direction, TradingMode

_IST_OFFSET = 19800
_SESSION_OPEN_MIN = 9 * 60 + 15          # 09:15 IST


@dataclass
class OrbParams:
    or_minutes: int = 15                 # length of the opening range
    rr: float = 1.5                      # target = rr x risk
    entry_cutoff_min: int = 11 * 60      # no new breakouts after this IST minute
    min_range_pct: float = 0.0           # skip days whose range is too tight (noise)
    max_range_pct: float = 10.0          # skip gap days that already moved
    allow_reversal: bool = False         # take an opposite break after a stop-out
    stop_at_mid: bool = False            # tighter stop: range midpoint instead of far side
    fade: bool = False                   # DIAGNOSTIC: trade AGAINST the break, same geometry


def _ist_min(ts: int) -> int:
    return ((ts + _IST_OFFSET) % 86400) // 60


def _day(ts: int) -> int:
    return (ts + _IST_OFFSET) // 86400


def run_orb(df: pd.DataFrame, p: OrbParams, symbol: str = "NIFTY",
            slippage_pts: float = 1.5) -> BacktestResult:
    if len(df) and not df["ts"].is_monotonic_increasing:
        df = df.sort_values("ts").reset_index(drop=True)
    slippage_pts = max(0.0, slippage_pts)

    ts = df["ts"].to_numpy()
    o, h, lo, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(df)

    # Group bar indices by session day (data is sorted, so days are contiguous).
    days: dict[int, list[int]] = {}
    for i in range(n):
        days.setdefault(_day(int(ts[i])), []).append(i)

    trades: list[BacktestTrade] = []

    for _, idx in days.items():
        or_end_min = _SESSION_OPEN_MIN + p.or_minutes
        or_bars = [i for i in idx if _ist_min(int(ts[i])) < or_end_min]
        rest = [i for i in idx if _ist_min(int(ts[i])) >= or_end_min]
        if not or_bars or not rest:
            continue

        or_high = float(max(h[i] for i in or_bars))
        or_low = float(min(lo[i] for i in or_bars))
        rng = or_high - or_low
        ref = float(c[or_bars[-1]])
        if rng <= 0 or ref <= 0:
            continue
        rng_pct = rng / ref * 100.0
        if rng_pct < p.min_range_pct or rng_pct > p.max_range_pct:
            continue                      # too tight to be signal / already gapped

        taken = 0
        k = 0
        while k < len(rest):
            i = rest[k]
            if _ist_min(int(ts[i])) > p.entry_cutoff_min:
                break

            long_break = c[i] > or_high
            short_break = c[i] < or_low
            if not (long_break or short_break):
                k += 1
                continue

            # Fill on the NEXT bar's open — never the breakout bar itself.
            if k + 1 >= len(rest):
                break
            e_i = rest[k + 1]
            is_long = bool(long_break)
            entry = float(o[e_i]) + (slippage_pts if is_long else -slippage_pts)
            stop = (
                (or_high + or_low) / 2.0 if p.stop_at_mid
                else (or_low if is_long else or_high)
            )
            risk = (entry - stop) if is_long else (stop - entry)
            if risk <= 2 * slippage_pts:   # cost-dominated; not a real setup
                k += 1
                continue

            if p.fade:
                # Same bar, same risk in points, opposite side. Holding the
                # geometry fixed isolates the DIRECTIONAL edge: if breakout is
                # -0.2R, fade should be near +0.2R minus the extra slippage. It
                # answers "does the open trend or revert", nothing more.
                is_long = not is_long
                entry = float(o[e_i]) + (slippage_pts if is_long else -slippage_pts)
                stop = entry - risk if is_long else entry + risk
            target = entry + p.rr * risk if is_long else entry - p.rr * risk

            # Walk the position forward to stop / target / session end.
            exit_px = None
            outcome = ""
            j_pos = k + 1
            while j_pos < len(rest):
                j = rest[j_pos]
                if is_long:
                    if lo[j] <= stop:
                        exit_px, outcome = stop, "stop"
                    elif h[j] >= target:
                        exit_px, outcome = target, "target"
                else:
                    if h[j] >= stop:
                        exit_px, outcome = stop, "stop"
                    elif lo[j] <= target:
                        exit_px, outcome = target, "target"
                if exit_px is None and j == rest[-1]:
                    exit_px, outcome = float(c[j]), "eod"
                if exit_px is not None:
                    break
                j_pos += 1
            if exit_px is None:
                break

            fill = exit_px - slippage_pts if is_long else exit_px + slippage_pts
            r = (fill - entry) / risk if is_long else (entry - fill) / risk
            trades.append(BacktestTrade(
                entry_ts=int(ts[e_i]), exit_ts=int(ts[rest[j_pos]]),
                direction=Direction.CE if is_long else Direction.PE,
                regime=f"ORB{p.or_minutes}", score=round(rng_pct, 2),
                entry=round(entry, 2), stop=round(stop, 2), target=round(target, 2),
                exit=round(fill, 2), r_multiple=round(r, 3), outcome=outcome,
            ))
            taken += 1
            if not p.allow_reversal or taken >= 2:
                break
            # Resume AT the exit bar, not after it. Its close is a legitimate
            # breakout signal and the fill would still be the following bar's
            # open, so no look-ahead is introduced — skipping it silently
            # delayed every reversal by one bar.
            k = j_pos

    return _summarize(df, trades, symbol, p)


def _summarize(df, trades: list[BacktestTrade], symbol: str, p: OrbParams) -> BacktestResult:
    rs = [t.r_multiple for t in trades]
    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple <= 0]
    ce = [t for t in trades if t.direction is Direction.CE]
    pe = [t for t in trades if t.direction is Direction.PE]

    def wr(g):
        return round(100 * sum(1 for t in g if t.r_multiple > 0) / len(g), 1) if g else 0.0

    equity, cum, peak, dd = [], 0.0, 0.0, 0.0
    for r in rs:
        cum = round(cum + r, 3)
        equity.append(cum)
        peak = max(peak, cum)
        dd = max(dd, peak - cum)

    sw = sum(t.r_multiple for t in wins)
    sl = sum(t.r_multiple for t in losses)
    return BacktestResult(
        symbol=symbol, mode=TradingMode.INTRADAY, timeframe=f"ORB{p.or_minutes}m",
        from_ts=int(df["ts"].iloc[0]) if len(df) else 0,
        to_ts=int(df["ts"].iloc[-1]) if len(df) else 0,
        bars=len(df), trades_total=len(trades), wins=len(wins), losses=len(losses),
        win_rate=round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        expectancy_r=round(sum(rs) / len(rs), 3) if rs else 0.0,
        avg_win_r=round(sw / len(wins), 3) if wins else 0.0,
        avg_loss_r=round(sl / len(losses), 3) if losses else 0.0,
        profit_factor=round(sw / abs(sl), 2) if sl < 0 else None,
        max_drawdown_r=round(dd, 3), total_r=round(sum(rs), 3),
        ce_trades=len(ce), ce_win_rate=wr(ce), pe_trades=len(pe), pe_win_rate=wr(pe),
        equity_curve=equity, trades=trades[-200:],
        note=(f"Opening Range Breakout, {p.or_minutes}m range, RR {p.rr}. Graded on the "
              "underlying future — theta, IV and premium spreads excluded, so real "
              "option results would be worse."),
    )
