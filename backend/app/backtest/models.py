"""Phase 5 backtest models.

This is a *directional* backtest: it replays the regime + technical score over
historical underlying (near-month future) candles and grades each signal on
whether the UNDERLYING reached its target-implied level or its invalidation
level first. It validates signal quality (win-rate, R-multiple, expectancy).

Not modelled here (they need historical option-premium data, which is a later
refinement): exact rupee option P&L, theta decay, IV, brokerage. The live-only
inputs — option-chain OI and news sentiment — are also absent from history, so
the backtest scores on the technical + regime components (price action, trend,
volume, volatility with historical VIX) and its pass gate is a technical-strength
threshold. It therefore selects a somewhat different (technically stricter) trade
population than the live engine — read results as a directional read on the
engine's edge, not live P&L.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from app.signals.models import Direction, TradingMode


class BacktestTrade(BaseModel):
    entry_ts: int
    exit_ts: int
    direction: Direction
    regime: str
    score: float                 # rescaled technical score at entry (0-100)
    entry: float
    stop: float
    target: float
    exit: float
    r_multiple: float            # net of slippage, in units of initial risk
    outcome: str                 # target / stop / time / eod


class BacktestResult(BaseModel):
    symbol: str
    mode: TradingMode
    timeframe: str
    from_ts: int
    to_ts: int
    bars: int
    trades_total: int
    wins: int
    losses: int
    win_rate: float              # %
    expectancy_r: float          # avg R per trade (the headline edge metric)
    avg_win_r: float
    avg_loss_r: float
    profit_factor: Optional[float] = None
    max_drawdown_r: float
    total_r: float
    ce_trades: int
    ce_win_rate: float
    pe_trades: int
    pe_win_rate: float
    equity_curve: list[float] = []   # cumulative R after each trade
    trades: list[BacktestTrade] = []
    note: str = ""


class BacktestRequest(BaseModel):
    symbol: str = "NIFTY"
    mode: TradingMode = TradingMode.INTRADAY
    days: int = 20               # calendar-day lookback (~0.7× trading sessions)
