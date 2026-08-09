"""Phase 3 data models: a manually-tracked trade + its lifecycle.

Tradewell never places orders — a Trade is created when the user clicks
"Mark as Entered" on a live signal, and represents a position they took manually.
The monitor updates its live premium / P&L / recommendation each cycle.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel

from app.signals.models import Direction, TradingMode


class TradeStatus(str, Enum):
    ENTERED = "entered"
    PARTIAL = "partial"      # partial profit booked, remainder running
    EXITED = "exited"
    IGNORED = "ignored"


class TradeAction(str, Enum):
    HOLD = "hold"
    BOOK_PARTIAL = "book_partial"
    MOVE_SL_ENTRY = "move_sl_entry"
    TRAIL_SL = "trail_sl"
    EXIT = "exit"
    TARGET1 = "target1_reached"
    TARGET2 = "target2_reached"
    STOPLOSS = "stop_loss_hit"
    INVALIDATED = "invalidated"
    TIME_EXIT = "time_exit"
    # Thesis-stall time stop: the trade ran its allotted minutes without ever
    # reaching the quick target — the thesis may merely be late, but in a
    # bought option "late" is a losing position wearing a hopeful face. Distinct
    # from TIME_EXIT (session close) so exit-type analytics can tell them apart.
    STALL = "stall_exit"


class TradeEvent(BaseModel):
    ts: int
    kind: str
    note: str


class Trade(BaseModel):
    id: str
    signal_id: Optional[str] = None
    symbol: str
    mode: TradingMode
    direction: Direction
    contract: str
    strike: float
    expiry: Optional[str] = None
    token: Optional[int] = None          # option instrument token (live premium)

    entry_premium: float
    lots: int
    lot_size: int
    quantity: int                        # lots * lot_size; SHRINKS on a partial
    # Size at entry, never mutated. book_partial reduces `quantity`, so it is
    # the wrong denominator for a return figure — dividing the full realised
    # P&L by the remainder inflates it (2 lots half-booked reads ~2x). None on
    # legacy rows, where `quantity` is still correct because no partial ran.
    initial_quantity: Optional[int] = None
    # Kite product the position was opened with. A protective SELL MUST carry
    # the same one: MIS and NRML are separate books, so a mismatched exit order
    # opens a NEW short leg instead of closing the long. None on legacy rows.
    product: Optional[str] = None        # "MIS" | "NRML"

    status: TradeStatus = TradeStatus.ENTERED
    stop_loss: float
    target1: float
    target2: float
    trailing_sl: float
    # Disaster backstop, computed from the ACTUAL fill. When set, this — not
    # premium_sl — is what ends the trade before Target 1; the index
    # invalidation is the primary exit. None = legacy premium-stop behaviour.
    disaster_sl: Optional[float] = None
    # Early partial-book level, from YOUR fill. `t0_hit` latches once reached:
    # from then on the stop is at entry, so the remainder runs risk-free and the
    # disaster backstop no longer applies.
    quick_target: Optional[float] = None
    t0_hit: bool = False
    # Premium OBSERVED on the cycle t0 latched — the fill a bank-at-the-quick-
    # target exit would actually get (a gap past the level fills at the tape,
    # not the level). The exit-policy A/B prices its counterfactual from this;
    # None on rows recorded before the field existed (A/B falls back to the
    # quick_target level, understating banking on gap-throughs — conservative).
    t0_cross_premium: Optional[float] = None
    invalidation_level: Optional[float] = None
    invalidation_dir: Optional[str] = None
    # STICKY INVALIDATION. When the underlying first breaks the level, the
    # moment is latched here and the recommendation stays INVALIDATED — even if
    # spot pops back inside — until the user explicitly acknowledges. On 23-Jul
    # a live row's advice silently reverted to "hold" after two invalidation
    # alerts, which legitimized ignoring them; the hold survived on luck.
    # `ack` newer than `fired` = acknowledged; a later re-break re-latches.
    invalidation_fired_at: Optional[int] = None
    invalidation_ack_at: Optional[int] = None
    # When spot was last OBSERVED back inside the level. The latch is
    # edge-triggered on this: an acknowledgment covers the whole continuous
    # breach (level-triggered re-latching nullified the ack within one monitor
    # cycle — each click bought ~5 seconds of silence); only a recovery
    # followed by a NEW break re-fires.
    invalidation_clear_at: Optional[int] = None
    # Post-close observation window (reversible auto-closes only). Kept OUT of
    # mfe/mae so the excursion evidence stays bounded by the trade's life; a
    # reopen folds these back in, because the trade turned out to be open the
    # whole time.
    post_close_mfe: Optional[float] = None
    post_close_mfe_at: Optional[int] = None
    post_close_mae: Optional[float] = None
    post_close_mae_at: Optional[int] = None
    # Where the exit price came from: "estimated" | "broker" | "simulated".
    # The UI banner must not claim "no order was placed" about a real fill.
    exit_price_source: Optional[str] = None
    # WHY the trade ended, in the trader's own words — asked for after a
    # broker-flat close that happened while the engine still said HOLD. Without
    # it the expectancy-by-exit-type report can't tell a disciplined broker-side
    # stop from a fear exit, and those two need opposite fixes.
    exit_reason: Optional[str] = None

    # Card's total score at fill. The overnight-hold ledger grades "hold into
    # close when positional score > X" — without the score ON the row, X could
    # only ever be guessed. None on rows recorded before the field existed.
    entry_score: Optional[float] = None
    # Tape state + GOLDEN label copied from the card at fill time (09-Aug):
    # the paper ledger splits fills by these to decide whether the golden
    # label (confirm-gated + developing tape + with the day) earns its
    # colour. None on rows recorded before the fields existed.
    tape_state: Optional[str] = None
    golden: Optional[bool] = None
    # First premium print of the first session AFTER entry, latched once by the
    # monitor. entry -> next_open isolates the overnight gap — the component no
    # intraday exit can manage (a gap settles before any stop can act), which
    # is exactly what the overnight-hold pattern is betting on. Rows that close
    # same-day never get one; a multi-day hold records only its FIRST morning
    # (later gaps happen inside the hold, not at the entry decision).
    next_open_premium: Optional[float] = None
    next_open_at: Optional[int] = None

    # --- excursion, recorded by the monitor while the position is OPEN ---
    # MFE = best premium seen, MAE = worst. Because they stop updating when the
    # trade closes, they are bounded by the trade's own life — so "MFE >= 15%"
    # on a stopped-out row means precisely "a 15% target would have caught this
    # BEFORE the stop did", which is the question worth answering.
    #
    # Sampled at the monitor's cadence (~5s), not tick by tick, so both are
    # conservative: the true extremes are at least this far out, never less.
    # Timestamp of the FIRST excursion observation, never overwritten. This is
    # the only reliable proof that tracking covered the whole trade: min(mfe_at,
    # mae_at) is NOT equivalent, because both move as new extremes arrive and a
    # trade that made a new high and then a new low reports neither's origin.
    excursion_from: Optional[int] = None
    mfe_premium: Optional[float] = None
    mfe_at: Optional[int] = None
    mae_premium: Optional[float] = None
    mae_at: Optional[int] = None

    # live, updated by the monitor
    current_premium: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    recommendation: TradeAction = TradeAction.HOLD
    recommendation_note: Optional[str] = None
    t1_hit: bool = False

    # lifecycle
    created_at: int
    entered_at: int
    exited_at: Optional[int] = None
    exit_premium: Optional[float] = None
    realized_pnl: float = 0.0            # booked P&L (partials + final)
    # True when Tradewell closed this row itself on a plan trigger rather than
    # you recording a fill. The premium is the live price at DETECTION, so it
    # is an estimate of your exit, not a confirmed one — correct it or reopen.
    auto_closed: bool = False
    auto_close_reason: Optional[str] = None
    # Last quantity Zerodha's position book reported for this contract. None
    # means never confirmed there — which is NOT the same as flat, and is why
    # absence alone never closes a row (you may simply not have bought yet).
    broker_qty: Optional[int] = None
    broker_checked_at: Optional[int] = None
    notes: Optional[str] = None
    events: list[TradeEvent] = []


# ---- request bodies ----
class EnterRequest(BaseModel):
    symbol: str
    mode: TradingMode = TradingMode.INTRADAY
    lots: int = 1
    entry_premium: Optional[float] = None   # actual fill; defaults to live premium
    # Defaults from the mode, but the user can override: an intraday signal is
    # often filled NRML, and the stop order has to match the fill, not the plan.
    product: Optional[str] = None           # "MIS" | "NRML"
    # The card the user clicked. If the active signal has changed by the time
    # the form is confirmed, the backend rejects instead of booking a contract
    # the user never intended to log.
    signal_id: Optional[str] = None
    # Set after the caller has been shown, and accepted, the rupee risk of a
    # size above the card's suggestion. The first submit is refused with that
    # number in the message; this field is the "yes, I know" on the retry.
    # Advisory only — it gates the WARNING, never the trade.
    acknowledge_oversize: bool = False


class ExitRequest(BaseModel):
    exit_premium: Optional[float] = None     # defaults to current live premium


class PartialRequest(BaseModel):
    exit_premium: Optional[float] = None
    fraction: float = 0.5                    # portion of remaining qty to book


class UpdateRequest(BaseModel):
    stop_loss: Optional[float] = None
    target1: Optional[float] = None
    target2: Optional[float] = None
    notes: Optional[str] = None
