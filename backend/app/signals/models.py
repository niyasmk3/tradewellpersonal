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
    # PAPER-ONLY until the paper book proves it: scalp cards cannot be
    # journaled live or handed to Kite unless SCALP_LIVE_ENABLED is set (the
    # gate documented to require 50+ honest-fill samples). Friction math is
    # the whole game at this cadence — see the cost-viability veto.
    SCALP = "scalp"


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
    # True when the OI/spread liquidity guards were BYPASSED by the ATM
    # fallback (P0-3 makes the card loud about it; the setup ledger refuses
    # such picks outright — review catch: the refusal read this flag off the
    # model while only a local variable in strike.py ever knew).
    guards_bypassed: bool = False


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
    # TRUTHFUL PROVENANCE. created_at is the card's immutable birth time; a
    # refresh stamps repriced_at instead. The old re-stamp produced trades
    # timestamped before their own card and displayed scores under the wrong
    # market state. Freshness guards read (repriced_at or created_at).
    repriced_at: Optional[int] = None
    # Pre-reprice ladder snapshots, oldest first — the audit trail of what the
    # user was actually looking at before each refresh. Capped small.
    reprice_history: list[dict] = []
    # TRUE lifetime reprice count. reprice_history rotates its middle out at
    # 10 slots, so len() plateaus there — this counter doesn't (04-Aug's 27
    # refreshes must read 27x, not 10x).
    reprice_total: int = 0
    # Live LTP of THIS card's option, attached fresh at request time (not
    # persisted) so the UI can show what the contract is trading at right now
    # against the entry zone. None when there is no live tick for the token.
    live_premium: Optional[float] = None

    # Lots implied by TRADING_CAPITAL x RISK_PER_TRADE_PCT and this card's own
    # premium stop. None when capital isn't configured — the tool must not
    # invent a size. Advisory only, like everything else here.
    suggested_lots: Optional[int] = None
    sizing_note: Optional[str] = None

    # AFFORDABILITY sizing, from the runtime-editable trading fund: how many
    # whole lots the fund buys at the freshest premium available. Recomputed
    # against live_premium on every poll, so the number the UI prefills tracks
    # the tape, not the issue-time reference. None when the fund is unset or no
    # premium/lot size is known. Distinct from suggested_lots on purpose: that
    # one caps LOSS, this one caps OUTLAY, and conflating them is how a "safe"
    # size deploys ten times the intended money.
    fund_lots: Optional[int] = None
    fund_qty: Optional[int] = None
    fund_note: Optional[str] = None

    # Scheduled-event caution (from the user-maintained .events.json): set when
    # a known macro event is near. Sizing is halved while it is set — the
    # documented conservative default before RBI/Budget/CPI/Fed windows.
    event_note: Optional[str] = None

    # Set when the volume/OI component floor vetoed this card from the LIVE
    # feed. Such a card exists only in the shadow store, where the paper book
    # fills it as a tagged counterfactual — the evidence that will prove (or
    # refute) the floor. A card with this set must never be offered to trade.
    hollow_reason: Optional[str] = None

    # TAPE STATE at birth (09-Aug study): how one-sided today's session was
    # when this card was born. "developing" = one-sided but 35-60% of the
    # day's range resolved, "stretched" = >=60% (late in a traveled move),
    # "two-way" = <=35% (chop). tape_aligned = card direction matches the
    # day's side. DISPLAY + LEDGER ONLY — never a gate, never a score input.
    tape_state: Optional[str] = None
    tape_resolved_pct: Optional[float] = None   # 0-100+, |close-open|/range
    tape_aligned: Optional[bool] = None
    # MACD(12,26,9) on the card's side of its signal line at birth — LOG-ONLY
    # (17-Sep sizing; live 30-fill readout decides if it earns a veto).
    macd_aligned: Optional[bool] = None
    # GOLDEN = every filter that measured positive stacked at once: a
    # confirm-gated mode, developing tape, direction with the day. A LABEL,
    # not a promise — the paper book grades golden vs ordinary fills and 30+
    # fills decide whether the label means anything (weekend sizing: the
    # components measured 49% / 85%-touch / 43%-clean-win on small samples;
    # the composite is UNPROVEN until its own ledger votes).
    golden: bool = False

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
