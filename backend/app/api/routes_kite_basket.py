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
from app.signals.modes import build_profiles
from app.signals.store import signal_store
from app.state import market_state

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


class BasketPayload(BaseModel):
    url: str
    api_key: str
    data: str           # JSON string, POSTed verbatim as the `data` form field
    summary: str        # human-readable confirmation line for the UI
    tradingsymbol: str
    quantity: int


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
    if now - card.created_at > _MAX_CARD_AGE_S:
        raise HTTPException(status_code=409, detail="Signal too old to one-click — re-check the premium first")

    profile = build_profiles(cfg).get(mode.value)
    if profile is None:
        raise HTTPException(status_code=409, detail=f"Mode '{mode.value}' is not enabled")

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
    price = math.ceil(float(card.entry_high) / _TICK) * _TICK
    price = round(price, 2)
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
