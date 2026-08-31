"""Kite Publisher hand-off ("offsite order execution").

Builds a pre-filled BUY basket for the currently-active signal and returns the
form fields the browser POSTs to Kite. Tradewell still places NO orders: the
payload opens Zerodha's own basket screen where the user reviews and confirms.
That confirmation step is the safety property — it must never be bypassed, so
`readonly` is deliberately false and no auto-submit happens server-side.

Spec: https://kite.trade/docs/connect/v3/basket/
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.kite.instruments import chain_key
from app.services import feed
from app.signals.models import TradingMode
from app.signals.modes import build_profiles, paper_only_block
from app.signals.store import signal_store
from app.state import market_state
from app.trades.store import trade_store

log = logging.getLogger("tradewell.kite")

router = APIRouter(prefix="/kite", tags=["kite"])

_BASKET_URL = "https://kite.zerodha.com/connect/basket"
# A card whose entry zone is this stale should not be one-click tradeable —
# the premium it was priced against no longer exists.
_MAX_CARD_AGE_S = 900
# NSE tick size for options — an unaligned LIMIT price is rejected.
_TICK = 0.05
# Kite's basket endpoint enforces max 8 chars on `tag` (the v3 docs say 20 —
# the API is the authority; "tradewell" was rejected with an InputException).
_TAG = "twell"
# Zerodha's RMS auto-squares MIS F&O positions around this time. After it, an
# intraday journal row that the user never marked exited describes a position
# that no longer exists — see `protect`.
_MIS_SQUAREOFF_MIN = 15 * 60 + 20        # ~15:20 IST
_IST_OFFSET = 19800


def _snap(x: float, up: bool) -> float:
    """Snap a price to the ₹0.05 tick, rounding `up` or down.

    Rounds the QUOTIENT before flooring: `x / 0.05` lands just below the true
    integer for ~35% of exact tick multiples (71.60 / 0.05 == 1431.9999999999998),
    so a naive floor drops an already-aligned price a full tick — loosening a
    stop below the level the plan set.
    """
    q = round(x / _TICK, 6)
    n = math.ceil(q) if up else math.floor(q)
    return round(n * _TICK, 2)


class BasketPayload(BaseModel):
    url: str
    api_key: str
    data: str           # JSON string, POSTed verbatim as the `data` form field
    summary: str        # human-readable confirmation line for the UI
    tradingsymbol: str
    quantity: int
    warning: Optional[str] = None   # shown before the hand-off, when it applies


class ResolvedContract(BaseModel):
    tradingsymbol: str
    lot_size: int
    expiry: Optional[str] = None


def _resolve_contract(
    symbol: str, expiry_key: str, token: Optional[int], strike: float, is_ce: bool
) -> Optional[ResolvedContract]:
    """Resolve the real Kite contract (e.g. NIFTY2672124200PE) for a card.

    Scoped to the card's OWN universe — `chain_key(symbol, expiry_key)`. An
    earlier version scanned every universe and fell back to a strike match
    inside the first one, which handed a positional (monthly) card the WEEKLY
    contract at the same strike, and could resolve a FINNIFTY card to a NIFTY
    symbol (the ladders overlap). Token match is tried across the whole
    universe before any strike fallback.
    """
    builder = feed.chain_builder
    if builder is None:
        return None
    universe = builder.universes.get(chain_key(symbol, expiry_key))
    if universe is None:
        return None

    pair = None
    if token is not None:                      # exact instrument first
        for sp in universe.strikes.values():
            if (sp.ce_token if is_ce else sp.pe_token) == token:
                pair = sp
                break
    if pair is None:                           # then the strike, same universe only
        pair = universe.strikes.get(float(strike))
    if pair is None:
        return None

    tsym = pair.ce_symbol if is_ce else pair.pe_symbol
    lot = pair.ce_lot_size if is_ce else pair.pe_lot_size
    if not tsym:
        return None
    return ResolvedContract(
        tradingsymbol=tsym, lot_size=lot,
        expiry=universe.expiry.isoformat() if universe.expiry else None,
    )


@router.get("/basket", response_model=BasketPayload)
def basket(
    symbol: str = Query("NIFTY"),
    mode: TradingMode = Query(TradingMode.INTRADAY),
    lots: int = Query(1, ge=1, le=100),
    signal_id: Optional[str] = Query(None),
) -> BasketPayload:
    cfg = get_settings()
    if not cfg.kite_api_key:
        raise HTTPException(status_code=409, detail="KITE_API_KEY not configured")

    resp = signal_store.latest(symbol, mode)
    if resp is None or resp.signal is None:
        raise HTTPException(status_code=409, detail="No active signal for this symbol/mode")
    card = resp.signal

    # Same guards as the journal: never hand Kite a contract the user didn't
    # actually look at, and never one priced off a dead card.
    if signal_id and signal_id != card.id:
        raise HTTPException(status_code=409, detail="The signal changed since you opened it — review the new card")
    if card.state != "active":
        raise HTTPException(status_code=409, detail="This signal is no longer active")
    now = int(time.time())
    if now >= card.valid_until:
        raise HTTPException(status_code=409, detail="Signal expired — entry window closed")
    # Freshness = when the card was last PRICED (a refresh counts); created_at
    # is the immutable birth time and deliberately never moves.
    if now - (card.repriced_at or card.created_at) > _MAX_CARD_AGE_S:
        raise HTTPException(status_code=409, detail="Signal too old to one-click — re-check the premium first")

    profile = build_profiles(cfg).get(mode.value)
    if profile is None:
        raise HTTPException(status_code=409, detail=f"Mode '{mode.value}' is not enabled")
    block = paper_only_block(mode, cfg)
    if block:
        raise HTTPException(status_code=409, detail=block)

    is_ce = card.direction.value == "CE"
    contract = _resolve_contract(symbol, profile.expiry_key, card.token, card.strike, is_ce)
    if contract is None:
        raise HTTPException(status_code=409, detail="Could not resolve the Kite contract — enter manually in Kite")

    # The resolved contract must be the one the card is priced against. If the
    # universe rolled (weekly expiry, monthly rollover) between card creation
    # and this click, refuse rather than hand over a different expiry.
    if card.expiry and contract.expiry and card.expiry != contract.expiry:
        raise HTTPException(
            status_code=409,
            detail=f"Contract expiry changed ({card.expiry} → {contract.expiry}) — re-check the card",
        )

    # Prefer the OPTION's own lot size; the future's (UnderlyingMeta) can lag
    # through an NSE lot revision.
    meta = market_state.underlyings.get(symbol.upper())
    lot_size = contract.lot_size or (meta.lot_size if meta else 0)
    if lot_size <= 0:
        raise HTTPException(status_code=409, detail="Lot size unknown — enter manually in Kite")
    quantity = lots * lot_size
    tsym = contract.tradingsymbol

    # LIMIT at the top of the entry zone: a MARKET order on an option can fill
    # far worse than the card's assumed premium, which would silently invalidate
    # the SL/target maths the whole signal is built on.
    #
    # Snap UP to the ₹0.05 NSE tick — an unaligned price is rejected outright,
    # and rounding up (not nearest) keeps the buy limit from landing below the
    # card's entry zone.
    price = _snap(float(card.entry_high), up=True)
    order = {
        "variety": "regular",
        "tradingsymbol": tsym,
        "exchange": "NFO",
        "transaction_type": "BUY",          # long premium only — never writes options
        "order_type": "LIMIT",
        "price": price,
        "quantity": quantity,
        "product": "MIS" if mode is TradingMode.INTRADAY else "NRML",
        "readonly": False,                  # the user MUST be able to review/edit
        "tag": _TAG,
    }
    summary = (
        f"BUY {quantity} ({lots}×{lot_size}) {tsym} @ LIMIT ₹{price} "
        f"· {order['product']} · review & confirm in Kite"
    )
    log.info("Kite basket prepared: %s", summary)
    return BasketPayload(
        url=_BASKET_URL, api_key=cfg.kite_api_key, data=json.dumps([order]),
        summary=summary, tradingsymbol=tsym, quantity=quantity,
    )


@router.get("/protect", response_model=BasketPayload)
def protect(trade_id: str = Query(...)) -> BasketPayload:
    """A resting stop-loss order for a position you ALREADY HOLD.

    WHY THIS IS A SEPARATE ENDPOINT AND NOT A SECOND LEG OF /basket:
    a SELL order sitting in the same basket as the BUY is submitted at the same
    moment. If the BUY does not fill (a gap through the limit, a partial) and
    price then trades through the trigger, the SELL executes on its own and you
    are SHORT a naked index option — undefined risk and lakhs of margin, from a
    button whose whole purpose was to reduce risk. So the protective order is
    only ever built from an open position in the journal, where the underlying
    long is known to exist.

    Uses order_type SL, not SL-M: NSE withdrew SL-M for index options in Sept
    2021 and Zerodha blocks it, so a stop must carry a limit price. That limit
    is placed a configurable margin BELOW the trigger so it behaves like a
    market exit instead of resting unfilled while the premium keeps falling.
    """
    cfg = get_settings()
    if not cfg.kite_api_key:
        raise HTTPException(status_code=409, detail="KITE_API_KEY not configured")

    trade = trade_store.get(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="No such trade")
    if trade.status.value not in ("entered", "partial"):
        raise HTTPException(
            status_code=409,
            detail="That position is already closed — a SELL order now would open a SHORT",
        )
    if trade.quantity <= 0:
        raise HTTPException(status_code=409, detail="Position has no remaining quantity")

    # An "entered" row only means the user told us they entered — nothing in
    # Tradewell can observe an exit made in Kite, and no code path closes a
    # trade automatically. For MIS that gap is not merely possible, it is
    # CERTAIN: Zerodha's RMS squares off intraday F&O around 15:20 IST, so any
    # intraday row from an earlier session (or from after the cutoff today)
    # describes a position the broker has already closed. Selling against it
    # would open a naked short, so refuse rather than lean on the warning.
    if trade.mode is TradingMode.INTRADAY:
        now_ist = int(time.time()) + _IST_OFFSET
        same_day = (trade.entered_at + _IST_OFFSET) // 86400 == now_ist // 86400
        before_cutoff = (now_ist % 86400) // 60 < _MIS_SQUAREOFF_MIN
        if not (same_day and before_cutoff):
            raise HTTPException(
                status_code=409,
                detail=("Intraday positions are auto-squared by Zerodha at ~15:20 IST, so "
                        "this journal entry is stale — you no longer hold it. Mark it exited; "
                        "a SELL now would open a SHORT."),
            )

    is_ce = trade.direction.value == "CE"
    profile = build_profiles(cfg).get(trade.mode.value)
    expiry_key = profile.expiry_key if profile else "nearest"
    contract = _resolve_contract(trade.symbol, expiry_key, trade.token, trade.strike, is_ce)
    if contract is None:
        raise HTTPException(status_code=409, detail="Could not resolve the Kite contract — place the SL manually")
    if trade.expiry and contract.expiry and trade.expiry != contract.expiry:
        raise HTTPException(
            status_code=409,
            detail=f"Contract expiry changed ({trade.expiry} → {contract.expiry}) — place the SL manually",
        )

    # The live stop, which may already have been trailed up after Target 1.
    stop = float(trade.trailing_sl or trade.stop_loss)
    if stop <= 0:
        raise HTTPException(status_code=409, detail="No stop level on this position")

    # A sell stop must sit BELOW the current premium; above it, Kite rejects the
    # order outright. Being here means the stop is already breached — the answer
    # is to exit now, not to rest an order that can never be placed.
    ltp = trade.current_premium
    if ltp is not None and ltp > 0 and stop >= ltp:
        raise HTTPException(
            status_code=409,
            detail=(f"Premium ₹{ltp:.2f} is already at/below the stop ₹{stop:.2f} — "
                    "this is an exit-now situation, not a resting stop"),
        )

    # Snap the trigger DOWN to the tick: rounding up would tighten the stop past
    # the level the plan set. The limit then sits a margin below the trigger.
    trigger = _snap(stop, up=False)
    limit = max(_TICK, _snap(trigger * (1.0 - cfg.kite_sl_limit_buffer_pct), up=False))
    if limit >= trigger:
        # Only reachable at a premium so small the buffer cannot be expressed in
        # ticks (a ₹0.05 stop). A "stop" there protects nothing and the order
        # would rest at its own trigger — say so instead of emitting it.
        raise HTTPException(
            status_code=409,
            detail=(f"Stop ₹{trigger:.2f} is too small for a limit below it — "
                    "the premium is near zero; exit manually instead"),
        )

    product = getattr(trade, "product", None) or (
        "MIS" if trade.mode is TradingMode.INTRADAY else "NRML"
    )
    order = {
        "variety": "regular",
        "tradingsymbol": contract.tradingsymbol,
        "exchange": "NFO",
        "transaction_type": "SELL",         # closes the long — never opens a short
        "order_type": "SL",                 # SL-M is blocked for index options
        "trigger_price": trigger,
        "price": limit,
        "quantity": trade.quantity,
        # MUST match the product the LONG was opened with — MIS and NRML are
        # separate books, so a mismatch opens a new short leg instead of
        # closing. Recorded at entry; mode is only a fallback for legacy rows.
        "product": product,
        "readonly": False,
        "tag": _TAG,
    }
    summary = (
        f"SELL {trade.quantity} {contract.tradingsymbol} · SL trigger ₹{trigger} "
        f"limit ₹{limit} · {order['product']} · review & confirm in Kite"
    )
    # Tradewell cannot read the Kite order book, so a repeat hand-off is the one
    # short-selling path left open: two SELL stops against one long means the
    # second one shorts. Journal it and escalate the warning on any repeat.
    prior = trade_store.note_stop_handoff(trade.id, trigger)
    warning = (
        "This is a SELL order. Submit it only while you actually hold this "
        f"position ({trade.quantity} qty) — if you have already exited, it opens a SHORT."
    )
    if not getattr(trade, "product", None):
        # Pre-dates the recorded product, so it is a guess from the mode.
        warning += (
            f"\n\nThis stop is built as {product}. It closes your position only if you "
            f"bought this contract as {product} in Kite — the other product would open "
            "a separate short leg."
        )
    if prior:
        warning = (
            f"⚠ You have ALREADY sent a stop for this position {prior} time(s). "
            "If that order is still live in Kite, adding another means TWO sell "
            "orders against one long — the second would open a SHORT. Cancel the "
            "existing one first, or close this dialog.\n\n" + warning
        )
    log.info("Kite protect basket prepared: %s (prior hand-offs: %d)", summary, prior)
    return BasketPayload(
        url=_BASKET_URL, api_key=cfg.kite_api_key, data=json.dumps([order]),
        summary=summary, tradingsymbol=contract.tradingsymbol,
        quantity=trade.quantity, warning=warning,
    )
