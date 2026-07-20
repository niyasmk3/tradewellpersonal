"""Backtest orchestration: resolve the mode profile, fetch history, run the engine.

Kept free of FastAPI types so it can be called from a thread (the route offloads
it via ``asyncio.to_thread`` — both the Kite fetch and the replay are blocking).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.backtest import data, engine
from app.backtest.models import BacktestRequest, BacktestResult
from app.config import get_settings
from app.kite.client import kite_service
from app.signals.modes import build_profiles
from app.state import market_state

log = logging.getLogger("tradewell.backtest")

_IST = timezone(timedelta(hours=5, minutes=30))
_MAX_DAYS = 90


class BacktestError(Exception):
    """User-actionable failure — surfaced to the client as a 422."""


def run_backtest(req: BacktestRequest) -> BacktestResult:
    cfg = get_settings()
    symbol = req.symbol.upper()

    if symbol not in cfg.signal_symbols:
        raise BacktestError(f"Backtest not enabled for {symbol} (enabled: {cfg.signal_symbols})")

    profiles = build_profiles(cfg)
    profile = profiles.get(req.mode.value)
    if profile is None:
        raise BacktestError(f"Mode '{req.mode.value}' not enabled (enabled: {list(profiles)})")

    days = max(3, min(_MAX_DAYS, req.days))

    if not kite_service.is_authenticated or kite_service.kite is None:
        raise BacktestError("Kite session not established — log in first")

    meta = market_state.underlyings.get(symbol)
    if meta is None or not meta.fut_token:
        raise BacktestError(
            f"No futures instrument wired for {symbol} — the live feed must run once to load instruments"
        )

    to_dt = datetime.now(_IST)
    from_dt = to_dt - timedelta(days=days)
    log.info("Backtest %s %s: fetch %s..%s @ %s", symbol, profile.mode.value,
             from_dt.date(), to_dt.date(), profile.timeframe)

    try:
        df = data.fetch_futures(kite_service.kite, meta.fut_token, from_dt, to_dt, profile.timeframe)
    except Exception as exc:
        raise BacktestError(
            f"Historical data fetch failed ({exc}). The Kite Historical Data add-on must be active."
        ) from exc

    if len(df) < profile.min_candles + 5:
        raise BacktestError(
            f"Not enough historical candles returned ({len(df)}). Try a longer window."
        )

    # Historical VIX is best-effort context (regime + volatility scoring). If it's
    # unavailable, the backtest proceeds without it (volatility falls back to neutral).
    vix_by_day = None
    if market_state.vix_token:
        try:
            vix_by_day = data.fetch_vix_daily(kite_service.kite, market_state.vix_token, from_dt, to_dt)
        except Exception as exc:
            log.warning("VIX history unavailable, proceeding without: %s", exc)

    result = engine.run(df, profile, symbol, cfg.backtest_slippage_pts,
                        cfg.backtest_max_hold_bars, vix_by_day)
    log.info("Backtest %s %s done: %d trades, expectancy %.2fR",
             symbol, profile.mode.value, result.trades_total, result.expectancy_r)
    return result
