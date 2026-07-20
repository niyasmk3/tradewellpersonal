"""Signal-engine REST endpoints (per trading mode)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.signals.models import SignalCard, SignalResponse, TradingMode

router = APIRouter(prefix="/signals", tags=["signals"])


def _resolve(symbol: str, mode: str) -> TradingMode:
    cfg = get_settings()
    if symbol.upper() not in cfg.signal_symbols:
        raise HTTPException(
            status_code=404,
            detail=f"Signals not enabled for {symbol} (enabled: {cfg.signal_symbols})",
        )
    # Must be both enabled in config AND a valid mode enum member.
    try:
        tmode = TradingMode(mode)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown mode '{mode}'")
    if mode not in cfg.signal_mode_list:
        raise HTTPException(
            status_code=404,
            detail=f"Mode '{mode}' not enabled (enabled: {cfg.signal_mode_list})",
        )
    return tmode


@router.get("/modes")
def enabled_modes() -> dict:
    # Only advertise modes that are both enabled in config and valid enum members.
    valid = {m.value for m in TradingMode}
    return {"modes": [m for m in get_settings().signal_mode_list if m in valid]}


@router.get("/{symbol}", response_model=SignalResponse)
def current_signal(symbol: str, mode: str = Query("intraday")) -> SignalResponse:
    # Import here to avoid a circular import at module load.
    from app.signals.store import signal_store

    tmode = _resolve(symbol, mode)
    latest = signal_store.latest(symbol, tmode)
    if latest is None:
        raise HTTPException(status_code=503, detail="Signal engine warming up — no evaluation yet")
    return latest


@router.get("/{symbol}/history", response_model=list[SignalCard])
def signal_history(symbol: str, mode: str = Query("intraday")) -> list[SignalCard]:
    from app.signals.store import signal_store

    tmode = _resolve(symbol, mode)
    return signal_store.history(symbol, tmode)
