"""Option-chain REST endpoint."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.kite.instruments import chain_key
from app.models.schemas import OptionChain
from app.state import market_state

router = APIRouter(prefix="/options", tags=["options"])


@router.get("/{symbol}", response_model=OptionChain)
def option_chain(symbol: str, expiry: str = Query("nearest", pattern="^(nearest|monthly)$")) -> OptionChain:
    chain = market_state.get_option_chain(chain_key(symbol, expiry))
    if chain is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {expiry} option chain for {symbol} yet (feed may still be warming up)",
        )
    return chain
