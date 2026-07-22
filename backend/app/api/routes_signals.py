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


@router.post("/{symbol}/reprice", response_model=SignalCard)
def reprice_signal(symbol: str, mode: str = Query("intraday")) -> SignalCard:
    """Re-price the active card against the CURRENT premium so it is orderable
    again — the same trade at today's price.

    A positional card stays valid for the session, but the Kite hand-off refuses
    anything older than 15 minutes because its entry zone was priced off a stale
    premium. This recomputes that zone (and the stop/targets) from the live LTP
    of the same strike, using the exact ladder the engine issues with, and
    re-stamps the card so the hand-off accepts it.
    """
    import time

    from app.signals import risk as risk_mod
    from app.signals.modes import build_profiles
    from app.signals.store import signal_store
    from app.state import market_state

    tmode = _resolve(symbol, mode)
    cfg = get_settings()
    resp = signal_store.latest(symbol, tmode)
    if resp is None or resp.signal is None:
        raise HTTPException(status_code=409, detail="No active signal to refresh")
    card = resp.signal
    if card.token is None:
        raise HTTPException(status_code=409, detail="This card has no tracked contract to re-price")

    # The live premium of the SAME strike. Refusing without it is the point —
    # a refresh that fell back to a stale reference would defeat itself.
    tick = market_state.ticks.get(card.token, {})
    ltp = tick.get("last_price")
    if not ltp or ltp <= 0:
        raise HTTPException(
            status_code=409,
            detail="No live premium for this strike right now — cannot re-price",
        )

    profile = build_profiles(cfg).get(mode)
    if profile is None:
        raise HTTPException(status_code=409, detail=f"Mode '{mode}' is not enabled")

    disaster_pct = (cfg.premium_disaster_pct
                    if cfg.stop_primary == "underlying" and cfg.trading_capital > 0 else None)
    ladder = risk_mod.price_ladder(
        float(ltp), profile.premium_sl_pct, profile.rr_target1, profile.rr_target2,
        disaster_pct=disaster_pct, quick_pct=cfg.quick_target_pct or None,
    )
    refreshed = signal_store.reprice_active(symbol, tmode, float(ltp), ladder, int(time.time()))
    if refreshed is None:
        raise HTTPException(status_code=409, detail="No active signal to refresh")
    return refreshed
