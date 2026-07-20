"""Phase 3 trade-journal endpoints (manual position tracking)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.signals.models import TradingMode
from app.signals.store import signal_store
from app.state import market_state
from app.trades.models import (
    EnterRequest,
    ExitRequest,
    PartialRequest,
    Trade,
    UpdateRequest,
)
from app.trades.store import trade_store

router = APIRouter(prefix="/trades", tags=["trades"])


def _live_premium(token: int | None) -> float | None:
    if token is None:
        return None
    return market_state.ticks.get(token, {}).get("last_price")


@router.get("", response_model=list[Trade])
def list_trades() -> list[Trade]:
    return trade_store.all()


@router.post("/enter", response_model=Trade)
def enter(body: EnterRequest) -> Trade:
    """Create a tracked trade from the currently-active signal for (symbol, mode)."""
    resp = signal_store.latest(body.symbol, body.mode)
    if resp is None or resp.signal is None:
        raise HTTPException(status_code=409, detail="No active signal to enter for this symbol/mode")
    card = resp.signal

    # The form was opened on a specific card — if the active signal flipped
    # meanwhile (e.g. CE cancelled, PE adopted), booking the new contract at the
    # old form's premium would journal a trade the user never took.
    if body.signal_id and body.signal_id != card.id:
        raise HTTPException(status_code=409, detail="The signal changed since you opened the form — review the new card")
    if card.state != "active":
        raise HTTPException(status_code=409, detail="This signal is no longer active")

    # One journal entry per signal — a double-click or stale tab must not
    # double-count P&L.
    if any(t.signal_id == card.id and t.status.value in ("entered", "partial")
           for t in trade_store.all()):
        raise HTTPException(status_code=409, detail="This signal is already entered — see Active Trades")

    # Premium preference: what the user typed > the LIVE premium right now >
    # the signal-creation reference (which can be minutes stale by entry time).
    entry = body.entry_premium
    if entry is None:
        entry = _live_premium(card.token) or card.ref_entry_premium or card.entry_low
    if not entry or entry <= 0:
        raise HTTPException(status_code=400, detail="Could not determine entry premium; pass entry_premium")

    meta = market_state.underlyings.get(body.symbol.upper())
    lot_size = meta.lot_size if meta and meta.lot_size else 1
    return trade_store.create_from_signal(card, body.lots, float(entry), lot_size)


@router.post("/{tid}/exit", response_model=Trade)
def exit_trade(tid: str, body: ExitRequest) -> Trade:
    existing = trade_store.get(tid)
    if existing is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    price = body.exit_premium if body.exit_premium is not None else _live_premium(existing.token)
    if price is None or price <= 0:  # a 0.0 tick is a dead quote, not a real fill
        raise HTTPException(status_code=400, detail="No exit premium available; pass exit_premium")
    updated = trade_store.exit_trade(tid, float(price))
    return updated  # type: ignore[return-value]


@router.post("/{tid}/partial", response_model=Trade)
def book_partial(tid: str, body: PartialRequest) -> Trade:
    existing = trade_store.get(tid)
    if existing is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    price = body.exit_premium if body.exit_premium is not None else _live_premium(existing.token)
    if price is None or price <= 0:
        raise HTTPException(status_code=400, detail="No premium available; pass exit_premium")
    updated = trade_store.book_partial(tid, float(price), body.fraction)
    if updated is None:
        raise HTTPException(status_code=409, detail="Trade is not open")
    return updated


@router.patch("/{tid}", response_model=Trade)
def update_trade(tid: str, body: UpdateRequest) -> Trade:
    updated = trade_store.update(tid, body.stop_loss, body.target1, body.target2, body.notes)
    if updated is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return updated


@router.post("/{tid}/ignore", response_model=Trade)
def ignore_trade(tid: str) -> Trade:
    updated = trade_store.ignore(tid)
    if updated is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return updated
