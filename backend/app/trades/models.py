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
    quantity: int                        # lots * lot_size
    # Kite product the position was opened with. A protective SELL MUST carry
    # the same one: MIS and NRML are separate books, so a mismatched exit order
    # opens a NEW short leg instead of closing the long. None on legacy rows.
    product: Optional[str] = None        # "MIS" | "NRML"

    status: TradeStatus = TradeStatus.ENTERED
    stop_loss: float
    target1: float
    target2: float
    trailing_sl: float
    invalidation_level: Optional[float] = None
    invalidation_dir: Optional[str] = None

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
