"""Pydantic response models shared across the REST + WebSocket API.

These are the contracts the Next.js frontend consumes. Keep them stable;
the signal-engine (Phase 2) will extend rather than reshape them.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Timeframe = Literal["1m", "3m", "5m", "15m"]


class Candle(BaseModel):
    ts: int  # epoch seconds (candle open time)
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndicatorSnapshot(BaseModel):
    """Latest value of each indicator for one timeframe."""
    vwap: Optional[float] = None
    ema9: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    rsi: Optional[float] = None
    atr: Optional[float] = None
    adx: Optional[float] = None
    supertrend: Optional[float] = None
    supertrend_dir: Optional[Literal["up", "down"]] = None
    bb_width: Optional[float] = None
    prev_day_high: Optional[float] = None
    prev_day_low: Optional[float] = None


class UnderlyingSnapshot(BaseModel):
    symbol: str                 # NIFTY / BANKNIFTY / FINNIFTY
    tradingsymbol: str          # e.g. "NIFTY 50"
    instrument_token: int
    ltp: Optional[float] = None
    change: Optional[float] = None       # points vs previous close
    change_pct: Optional[float] = None
    day_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    prev_close: Optional[float] = None
    updated_at: Optional[int] = None     # epoch seconds of last tick
    # Near-month future: has real volume, so it powers VWAP / volume indicators.
    fut_token: Optional[int] = None
    fut_ltp: Optional[float] = None
    fut_tradingsymbol: Optional[str] = None


class OptionRow(BaseModel):
    strike: float
    # Call side
    ce_token: Optional[int] = None
    ce_ltp: Optional[float] = None
    ce_oi: Optional[float] = None
    ce_oi_change: Optional[float] = None   # vs previous poll snapshot
    ce_volume: Optional[float] = None
    ce_iv: Optional[float] = None
    # Put side
    pe_token: Optional[int] = None
    pe_ltp: Optional[float] = None
    pe_oi: Optional[float] = None
    pe_oi_change: Optional[float] = None
    pe_volume: Optional[float] = None
    pe_iv: Optional[float] = None


class OptionChain(BaseModel):
    symbol: str
    expiry: Optional[str] = None          # ISO date of the weekly expiry in use
    atm_strike: Optional[float] = None
    pcr: Optional[float] = None           # put-call ratio (sum PE OI / sum CE OI)
    rows: list[OptionRow] = []
    updated_at: Optional[int] = None


class VixSnapshot(BaseModel):
    ltp: Optional[float] = None
    change_pct: Optional[float] = None
    status: Optional[str] = None          # "Calm" / "Stable" / "Elevated" / "High"


class MarketSnapshot(BaseModel):
    """The full payload pushed to the browser on each broadcast tick."""
    server_time: int
    market_open: bool
    underlyings: list[UnderlyingSnapshot] = []
    vix: Optional[VixSnapshot] = None
    # Feed health: seconds since the newest underlying tick, and whether the
    # data should be treated as stale (market open but ticks stopped flowing).
    last_tick_age: Optional[int] = None
    feed_stale: bool = False
    # OBSERVED FACT, not configuration: True only when this feed start actually
    # dispatched its verification push through the alert webhook. False means
    # a new signal will NOT reach the phone — the header must say so.
    alerts_armed: bool = False


class AuthStatus(BaseModel):
    authenticated: bool
    api_key_configured: bool
    login_url: Optional[str] = None
    user_id: Optional[str] = None
    ticker_connected: bool = False
    instruments_loaded: bool = False
    message: Optional[str] = None


class SessionRequest(BaseModel):
    request_token: str
