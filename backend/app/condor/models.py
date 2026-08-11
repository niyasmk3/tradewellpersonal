"""Pydantic contracts for the Iron Condor module.

Deliberately separate from signals/models.py: that hierarchy encodes the
single-leg long-premium invariant (Direction CE|PE, one strike, premium-falls
stops) and the Phase-1 audit's conclusion was that a condor must be new code,
not a sign-flipped retrofit. Nothing here is imported by the directional
engine and vice versa (shared enums are re-declared, not reused, so a future
change to one engine cannot silently re-price the other's cards).
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LegSide(str, Enum):
    SELL = "SELL"
    BUY = "BUY"


class CondorLeg(BaseModel):
    side: LegSide
    right: str                              # "CE" | "PE"
    strike: float
    token: Optional[int] = None
    tradingsymbol: Optional[str] = None
    ltp: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    spread_pct: Optional[float] = None
    oi: Optional[float] = None
    iv: Optional[float] = None              # fraction, e.g. 0.143
    delta: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    gamma: Optional[float] = None
    quote_age_s: Optional[int] = None


class ScorePart(BaseModel):
    name: str
    points: float
    max: float
    detail: str = ""


class CondorCard(BaseModel):
    id: str
    symbol: str
    expiry: Optional[str] = None            # ISO date
    dte: int = 0
    dte_trading: int = 0
    spot: Optional[float] = None
    state: str = "active"                   # active / expired / withdrawn
    trial: bool = True                      # shadow-first: rendered, never pushed

    legs: list[CondorLeg] = Field(default_factory=list)
    width: float = 0.0                      # wing width, points (same both sides)
    lot_size: int = 0
    lots: int = 1

    # Credit economics (per unit; rupee fields already × lot × lots)
    credit_mid: float = 0.0                 # points at mid
    credit_ideal_low: float = 0.0
    credit_ideal_high: float = 0.0
    credit_min_acceptable: float = 0.0
    credit_avoid_below: float = 0.0
    max_profit: float = 0.0                 # rupees, net of est. charges
    max_loss: float = 0.0                   # rupees, incl. est. charges
    be_low: float = 0.0
    be_high: float = 0.0
    pop: Optional[float] = None             # 0..1
    risk_reward: Optional[float] = None
    margin_estimate: Optional[float] = None # rupees, LABELLED estimate
    charges_estimate: float = 0.0

    net_delta: Optional[float] = None       # per structure, 1 lot
    net_theta: Optional[float] = None
    net_vega: Optional[float] = None
    net_gamma: Optional[float] = None

    # Context at issue
    regime: str = ""
    regime_confidence: int = 0
    breakout_band: str = ""
    breakout_score: int = 0
    vol_regime: str = ""
    vix: Optional[float] = None
    vix_percentile: Optional[float] = None
    iv_over_rv: Optional[float] = None
    em_primary: Optional[float] = None
    em_straddle: Optional[float] = None
    em_iv: Optional[float] = None
    em_atr: Optional[float] = None
    em_rv: Optional[float] = None
    range_low: Optional[float] = None
    range_high: Optional[float] = None

    score: float = 0.0
    score_parts: list[ScorePart] = Field(default_factory=list)
    quality: str = "NO TRADE"               # HIGH QUALITY / WATCHLIST / NO TRADE
    reasons: list[str] = Field(default_factory=list)      # ✓ positives
    risks: list[str] = Field(default_factory=list)        # ⚠ negatives

    created_at: int = 0
    valid_until: int = 0
    updated_at: int = 0


class CondorPosition(BaseModel):
    """A manually executed condor the user journals for monitoring. Advisory
    only — Tradewell never places or exits the orders."""
    id: str
    card_id: Optional[str] = None
    symbol: str
    expiry: Optional[str] = None
    legs: list[CondorLeg] = Field(default_factory=list)   # entry fills in .ltp
    width: float = 0.0
    lot_size: int = 0
    lots: int = 1
    credit_fill: float = 0.0                # points collected at entry
    # All charges actually incurred so far: entry orders at build time, plus
    # each journaled adjustment's close+open orders. Live and realized P&L
    # both subtract this, so the two can never disagree by the sunk friction
    # (review finding: live P&L overstated by the entry charges).
    charges_accrued: float = 0.0
    status: str = "open"                    # open / closed
    entered_at: int = 0
    exited_at: Optional[int] = None
    exit_debit: Optional[float] = None      # points paid to close
    exit_reason: Optional[str] = None
    realized_pnl: Optional[float] = None    # rupees, net of charges
    adjustments: int = 0
    notes: Optional[str] = None
    # Combined-premium excursions (points): min = best (most profit), max = worst.
    prem_min: Optional[float] = None
    prem_max: Optional[float] = None


class PositionView(BaseModel):
    position: CondorPosition
    combined_mid: Optional[float] = None    # points to close now
    pnl: Optional[float] = None             # rupees net of est. exit charges
    pnl_pct_of_max: Optional[float] = None
    captured_pct: Optional[float] = None    # 1 - combined/credit
    dist_short_ce: Optional[float] = None
    dist_short_pe: Optional[float] = None
    dist_short_ce_em: Optional[float] = None  # ÷ remaining EM
    dist_short_pe_em: Optional[float] = None
    net_delta: Optional[float] = None
    net_theta: Optional[float] = None
    health: Optional[int] = None
    health_band: Optional[str] = None       # Healthy / Warning / Critical
    status_advice: str = "HOLD"
    status_detail: str = ""
    breakout_band: Optional[str] = None
    adjustment: Optional[dict] = None       # suggested adjustment, when any
    # Per-leg quality refusals (stale quote, no depth, expiry not subscribed).
    # Non-empty means premium-driven advice was withheld, not that all is well.
    data_problems: list = Field(default_factory=list)


class CondorResponse(BaseModel):
    symbol: str
    evaluated_at: int
    data_ok: bool = True
    data_problems: list[str] = Field(default_factory=list)
    regime: str = ""
    regime_confidence: int = 0
    regime_votes: dict = Field(default_factory=dict)
    regime_vetoes: list[str] = Field(default_factory=list)
    breakout_band: str = ""
    breakout_score: int = 0
    breakout_parts: list[str] = Field(default_factory=list)
    vol_regime: str = ""
    vix: Optional[float] = None
    vix_percentile: Optional[float] = None
    em_straddle: Optional[float] = None
    em_iv: Optional[float] = None
    em_atr: Optional[float] = None
    em_rv: Optional[float] = None
    em_primary: Optional[float] = None
    iv_over_rv: Optional[float] = None
    expected_low: Optional[float] = None
    expected_high: Optional[float] = None
    spot: Optional[float] = None
    expiry: Optional[str] = None
    dte: Optional[int] = None
    card: Optional[CondorCard] = None
    no_trade_reasons: list[str] = Field(default_factory=list)


class EnterCondorRequest(BaseModel):
    card_id: Optional[str] = None
    symbol: str = "NIFTY"
    expiry: Optional[str] = None
    lots: int = Field(default=1, ge=1, le=100)
    # Entry fills, points per unit. Shorts positive premium received.
    short_ce_strike: float
    short_ce_fill: float
    short_pe_strike: float
    short_pe_fill: float
    wing_ce_strike: float
    wing_ce_fill: float
    wing_pe_strike: float
    wing_pe_fill: float
    notes: Optional[str] = None


class ExitCondorRequest(BaseModel):
    exit_debit: float = Field(ge=0)         # points paid to close the structure
    reason: str = "manual"


class AdjustCondorRequest(BaseModel):
    """Journal a manually-executed roll of one side (spec §12 candidate A).
    Advisory app: the user already did this in Kite; we record the arithmetic
    so credit/max-loss/charges/the adjustment cap stay truthful."""
    side: str = Field(pattern="^(CE|PE)$")
    close_debit: float = Field(ge=0)        # points paid to close the old spread
    new_short_strike: float
    new_short_fill: float = Field(gt=0)
    new_wing_strike: float
    new_wing_fill: float = Field(ge=0)


class WhatIfRequest(BaseModel):
    card_id: Optional[str] = None
    position_id: Optional[str] = None
    spots: list[float] = Field(default_factory=list)      # extra user spots
    iv_shifts: list[float] = Field(default=[-0.10, 0.0, 0.10])
    days_forward: list[int] = Field(default=[0, 1, 2])
