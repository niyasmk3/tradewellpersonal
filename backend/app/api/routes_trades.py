"""Phase 3 trade-journal endpoints (manual position tracking)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings

from app.signals.models import TradingMode
from app.signals.modes import ladder_params
from app.signals.risk import price_ladder
from app.signals.risk_limits import risk_limit_store
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


@router.get("/excursion")
def excursion(source: str = "live") -> dict:
    """How far trades actually ran before ending — the target/stop evidence.

    `source=paper` reads the simulated book, which accumulates far faster than
    the live one and is the intended way to gather a usable sample.
    """
    from app.trades import excursion as exc

    if source == "paper":
        from app.services import feed
        store = getattr(feed, "paper_store", None)
        if store is None:
            raise HTTPException(status_code=409, detail="Paper trading is off")
        return exc.target_curve(store.all())
    return exc.target_curve(trade_store.all())


@router.get("/analytics")
def analytics(source: str = "live") -> dict:
    """Expectancy and capture efficiency grouped by HOW trades ended.

    `source=paper` reads the simulated book with BOTH of its summary's
    disciplines: honest-fill era only (pre-23-Jul fills were fictional) AND
    net of the Zerodha charge model — a gross paper expectancy flips sign at
    exactly the scalp cadence where the SCALP_LIVE_ENABLED gate consults this
    number. The live journal stays gross, matching what it records; the
    payload's `pnl_basis` states which basis applies either way.
    """
    from app.trades import analytics as ana

    if source == "paper":
        from app.paper import charges as chg
        from app.paper.service import HONEST_FILLS_FROM
        from app.services import feed
        store = getattr(feed, "paper_store", None)
        if store is None:
            raise HTTPException(status_code=409, detail="Paper trading is off")
        rows = [t for t in store.all() if t.entered_at >= HONEST_FILLS_FROM]
        return ana.summarize(
            rows,
            # legs=1 on a missing exit premium mirrors charges.py's lapse rule.
            charges_fn=lambda t: chg.charges(
                t.entry_premium, t.exit_premium or 0.0,
                t.initial_quantity or t.quantity, 2 if t.exit_premium else 1),
        )
    return ana.summarize(trade_store.all())


class ExitReasonRequest(BaseModel):
    reason: str


@router.post("/{tid}/exit-reason", response_model=Trade)
def exit_reason(tid: str, body: ExitReasonRequest) -> Trade:
    """Record why a closed trade ended — the input the exit-type report needs."""
    if trade_store.get(tid) is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    updated = trade_store.set_exit_reason(tid, body.reason)
    if updated is None:
        raise HTTPException(status_code=409, detail="Trade is not closed (or reason was empty)")
    return updated


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

    # The OPTION contract's own lot size, which the card now carries, not the
    # future's: after an NSE lot revision the two disagree, and the journal
    # quantity must equal what was actually bought — the protective stop is
    # sized from it.
    meta = market_state.underlyings.get(body.symbol.upper())
    lot_size = card.lot_size or (meta.lot_size if meta and meta.lot_size else 1)
    product = (body.product or "").upper() or ("MIS" if card.mode is TradingMode.INTRADAY else "NRML")
    if product not in ("MIS", "NRML"):
        raise HTTPException(status_code=400, detail="product must be MIS or NRML")
    cfg = get_settings()
    # Scalp cards are PAPER-ONLY until the paper book earns the flip: at scalp
    # cadence the charges model shows friction eating 20-30%% of gross, and the
    # SEBI 93%%-lose statistic is disproportionately made of option scalpers.
    if card.mode is TradingMode.SCALP and not cfg.scalp_live_enabled:
        raise HTTPException(
            status_code=409,
            detail=("Scalp mode is paper-only: let the paper book accumulate 50+ "
                    "honest fills, then set SCALP_LIVE_ENABLED=true if the expectancy survives."))
    # Same gate as the card: no configured capital means sizing cannot shrink
    # the position to pay for a wider stop, so the premium stop keeps governing.
    disaster_pct = (cfg.premium_disaster_pct
                    if cfg.stop_primary == "underlying" and cfg.trading_capital > 0 else None)
    # The mode's own stop/target geometry, so the journal re-prices the ladder
    # against the fill instead of inheriting levels drawn for another premium.
    params = ladder_params(cfg, card.mode)
    sl_pct, rr1, rr2 = params if params else (None, None, None)

    _guard_size(body, card, float(entry), lot_size, sl_pct, rr1, rr2, disaster_pct, cfg)

    return trade_store.create_from_signal(
        card, body.lots, float(entry), lot_size, product, disaster_pct=disaster_pct,
        quick_pct=cfg.quick_target_pct or None, sl_pct=sl_pct, rr1=rr1, rr2=rr2)


def _guard_size(body: EnterRequest, card, entry: float, lot_size: int,
                sl_pct: float | None, rr1: float | None, rr2: float | None,
                disaster_pct: float | None, cfg) -> None:
    """Refuse a first attempt to journal more lots than the card suggested.

    NOT a hard block: `/trades/enter` records a fill that already happened at the
    broker, so refusing outright would only produce an untracked position, which
    is strictly worse. It refuses ONCE, with the rupee risk spelled out, and the
    caller re-submits with `acknowledge_oversize`.

    This is the guard that was missing on 20-Jul: ten-lot sizes against a card
    suggesting one or two turned three stop-outs into -₹54,437, and the same
    override reappeared on 22-Jul at 31% of the daily limit on a single trade.
    """
    suggested = card.suggested_lots or 0
    if body.acknowledge_oversize or suggested <= 0 or body.lots <= suggested:
        return

    # Risk against the stop that ACTUALLY ends the trade, re-priced from the
    # fill by the same function the journal uses. With the underlying stop
    # primary that is the disaster backstop, not the premium stop — sizing off
    # the narrower one understates the acknowledged rupees ~2.5x, which on a
    # 10-lot entry is the difference between "₹11,700" and "₹29,250" in the
    # dialog the trader clicks through.
    if sl_pct:
        ladder = price_ladder(entry, sl_pct, rr1 or 0, rr2 or 0, disaster_pct=disaster_pct)
        stop = ladder["disaster_sl"] or ladder["premium_sl"]
    else:
        stop = card.disaster_sl or card.premium_sl
    risk = max(entry - stop, 0.0) * body.lots * lot_size
    limit = risk_limit_store.effective(cfg).get("daily_loss_limit", 0.0)
    share = f", {risk / limit * 100:.0f}% of your ₹{limit:,.0f} daily loss limit" if limit > 0 else ""
    # A structured detail so the UI can offer "journal it anyway" instead of
    # showing a dead-end error — the point is a speed bump, not a wall.
    raise HTTPException(
        status_code=409,
        detail={
            "code": "oversize_lots",
            "message": (
                f"{body.lots} lots is above the card's suggested {suggested}. "
                f"At ₹{entry} with the stop at ₹{stop}, that risks ₹{risk:,.0f}{share}."
            ),
            "lots": body.lots,
            "suggested_lots": suggested,
            "risk_rupees": round(risk, 2),
        },
    )


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


@router.post("/{tid}/reopen", response_model=Trade)
def reopen(tid: str) -> Trade:
    """Reverse an auto-close: you are in fact still holding this position.

    Tradewell closes rows on plan triggers without seeing your broker, so the
    close is an inference. Only auto-closed rows can be reopened — a fill you
    reported yourself is a fact, not a guess.
    """
    t = trade_store.reopen(tid)
    if t is None:
        raise HTTPException(
            status_code=409,
            detail="Only an auto-closed trade can be reopened",
        )
    return t


@router.post("/{tid}/ack-invalidation", response_model=Trade)
def ack_invalidation(tid: str) -> Trade:
    """Acknowledge a broken thesis and keep holding by explicit choice.

    Until this is called, an invalidated open row's recommendation stays
    INVALIDATED (even if spot recovers) and the phone re-alerts every 10
    minutes. The ack covers THIS break; a later re-break re-latches.
    """
    updated = trade_store.ack_invalidation(tid)
    if updated is None:
        raise HTTPException(status_code=404, detail="Trade not found or no invalidation to acknowledge")
    return updated


@router.post("/{tid}/ignore", response_model=Trade)
def ignore_trade(tid: str) -> Trade:
    updated = trade_store.ignore(tid)
    if updated is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return updated
