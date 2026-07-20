"""Market-data REST endpoints: snapshot, candles, indicators, market mood."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.market.candles import TIMEFRAME_SECONDS
from app.market.indicators import compute_snapshot
from app.market.mood import MarketMood, get_market_mood
from app.models.schemas import Candle, IndicatorSnapshot, MarketSnapshot
from app.state import market_state
from app.api.ws import build_snapshot

router = APIRouter(prefix="/market", tags=["market"])


def _require_symbol(symbol: str):
    meta = market_state.underlyings.get(symbol.upper())
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Unknown or untracked underlying: {symbol}")
    return meta


def _require_tf(tf: str) -> str:
    if tf not in TIMEFRAME_SECONDS:
        raise HTTPException(status_code=400, detail=f"tf must be one of {list(TIMEFRAME_SECONDS)}")
    return tf


@router.get("/snapshot", response_model=MarketSnapshot)
def snapshot() -> MarketSnapshot:
    """Same payload the WebSocket pushes — handy for initial load / debugging."""
    return build_snapshot()


@router.get("/mood", response_model=Optional[MarketMood])
def market_mood() -> Optional[MarketMood]:
    """Market Mood Index (Tickertape Fear/Greed gauge). Read-only context; cached
    and best-effort — returns null if disabled or the source is unreachable."""
    if not get_settings().mmi_enabled:
        return None
    return get_market_mood()


@router.get("/{symbol}/candles", response_model=list[Candle])
def candles(symbol: str, tf: str = Query("3m"), limit: int = Query(240, le=600)) -> list[Candle]:
    _require_symbol(symbol)
    _require_tf(tf)
    engine = market_state.engine_for_symbol(symbol)
    if engine is None:
        return []
    return engine.snapshot(tf, limit=limit)


@router.get("/{symbol}/indicators", response_model=IndicatorSnapshot)
def indicators(symbol: str, tf: str = Query("3m")) -> IndicatorSnapshot:
    _require_symbol(symbol)
    _require_tf(tf)
    engine = market_state.engine_for_symbol(symbol)
    if engine is None:
        return IndicatorSnapshot()
    return compute_snapshot(engine.dataframe(tf))
