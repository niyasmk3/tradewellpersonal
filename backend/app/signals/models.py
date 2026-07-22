"""Signal-engine data models (the Phase 2 contracts).

A signal flows: MarketRegime -> ScoreBreakdown -> (direction) -> StrikePick ->
RiskPlan -> SignalCard. The frontend renders SignalResponse: a market-status
header plus either an active SignalCard or a no-trade / wait state.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TradingMode(str, Enum):
    INTRADAY = "intraday"
    POSITIONAL = "positional"


class Regime(str, Enum):
    STRONG_BULLISH = "strong_bullish"
    MODERATE_BULLISH = "moderate_bullish"
    STRONG_BEARISH = "strong_bearish"
    MODERATE_BEARISH = "moderate_bearish"
    SIDEWAYS = "sideways"
    COMPRESSION = "compression"
    BREAKOUT_DEVELOPING = "breakout_developing"
    NEWS_VOLATILITY = "news_volatility"
    REVERSAL = "reversal"
    UNSAFE = "unsafe"
    WARMING_UP = "warming_up"  # not enough candles yet


class Bias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class Direction(str, Enum):
    CE = "CE"
    PE = "PE"
    NONE = "NONE"


class Action(str, Enum):
    BUY_CE = "buy_ce"
    BUY_PE = "buy_pe"
    WAIT = "wait"          # setup forming, wait for confirmation
    AVOID = "avoid"        # no trade


class SignalState(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"


class ScoreComponent(BaseModel):
    name: str
    points: float
    max: float
    reasons: list[str] = []


class ScoreBreakdown(BaseModel):
    direction: Direction
    components: list[ScoreComponent] = []
    total: float = 0.0
    max: float = 100.0

    @property
    def label(self) -> str:
        if self.total >= 80:
            return "strong"
        if self.total >= 70:
            return "valid"
        if self.total >= 60:
            return "wait"
        return "no_trade"


class RegimeResult(BaseModel):
    regime: Regime
    bias: Bias
    strength: float = 0.0          # 0-1 conviction in the regime
    tradeable: bool = False        # False for sideways/compression/unsafe/etc.
    notes: list[str] = []


class StrikePick(BaseModel):
    strike: float
    option_type: Direction
    tradingsymbol: Optional[str] = None
    token: Optional[int] = None
    ltp: Optional[float] = None
    oi: Optional[float] = None
    moneyness: str = "ATM"          # ITM / ATM / OTM
    rationale: list[str] = []
    considered: list[str] = []      # human-readable "why not the others"


class RiskPlan(BaseModel):
    entry_low: float
    entry_high: float
    premium_sl: float
    # The level that actually ends the trade before Target 1 when the index
    # invalidation is primary. premium_sl stays the RISK UNIT that targets and
    # sizing are derived from; this is the backstop the monitor acts on.
    disaster_sl: Optional[float] = None
    # Early partial-book level; None disables it.
    quick_target: Optional[float] = None
    target1: float
    target2: float
    trailing_sl_rule: str
    risk_reward: float              # to target1
    underlying_invalidation: str    # e.g. "NIFTY must stay above 24,710"
    invalidation_note: str
    invalidation_level: float       # numeric index level for monitoring
    invalidation_dir: str           # "above" (CE) | "below" (PE)


class SignalCard(BaseModel):
    id: str
    symbol: str
    mode: TradingMode = TradingMode.INTRADAY
    title: str                      # e.g. "NIFTY BULLISH BREAKOUT"
    action: Action
    direction: Direction
    state: SignalState = SignalState.ACTIVE

    contract: str                   # e.g. "NIFTY 24800 CE"
    strike: float
    token: Optional[int] = None     # option instrument token (live premium tracking)
    expiry: Optional[str] = None

    entry_low: float
    entry_high: float
    premium_sl: float
    disaster_sl: Optional[float] = None
    quick_target: Optional[float] = None
    target1: float
    target2: float
    trailing_sl_rule: str
    risk_reward: float
    confidence: float               # == score.total

    underlying_invalidation: str
    invalidation_note: str
    invalidation_level: Optional[float] = None
    invalidation_dir: Optional[str] = None
    reasons: list[str] = []

    created_at: int
    valid_until: int
    score: ScoreBreakdown

    # reference values captured at creation (used by Phase 3 monitoring)
    ref_spot: Optional[float] = None
    ref_entry_premium: Optional[float] = None
    # Live LTP of THIS card's option, attached fresh at request time (not
    # persisted) so the UI can show what the contract is trading at right now
    # against the entry zone. None when there is no live tick for the token.
    live_premium: Optional[float] = None

    # Lots implied by TRADING_CAPITAL x RISK_PER_TRADE_PCT and this card's own
    # premium stop. None when capital isn't configured — the tool must not
    # invent a size. Advisory only, like everything else here.
    suggested_lots: Optional[int] = None
    sizing_note: Optional[str] = None

    # Contract multiplier, so the UI can turn premium levels into rupees BEFORE
    # the trade is placed. 0/None means the instrument dump hasn't loaded a lot
    # size — the UI must then show no rupee figures rather than guess one.
    lot_size: Optional[int] = None
    # Risk budget context, for relating a position's loss to the account.
    trading_capital: Optional[float] = None
    daily_loss_limit: Optional[float] = None


class MarketStatus(BaseModel):
    symbol: str
    mode: TradingMode = TradingMode.INTRADAY
    regime: Regime
    regime_label: str               # human phrase e.g. "Moderate Bullish"
    bias: Bias
    bull_score: float               # 0-100 bullish setup score
    bear_score: float               # 0-100 bearish setup score
    headline: str                   # e.g. "Search for CE opportunity"
    vix_status: Optional[str] = None
    news_label: Optional[str] = None    # positive / negative / neutral / volatile
    news_net: Optional[float] = None    # -100..+100
    notes: list[str] = []


class SignalResponse(BaseModel):
    symbol: str
    mode: TradingMode = TradingMode.INTRADAY
    evaluated_at: int
    status: MarketStatus
    action: Action
    signal: Optional[SignalCard] = None
    no_trade_reason: Optional[str] = None
    score: Optional[ScoreBreakdown] = None   # chosen-direction breakdown
