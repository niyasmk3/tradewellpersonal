"""Signal-engine REST endpoints (per trading mode)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

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
    # Attach the live premium at request time (not at issue) so it ticks with
    # each poll — latest() already returned a deep copy, so this is safe.
    if latest.signal is not None and latest.signal.token is not None:
        from app.config import get_settings
        from app.signals.risk_limits import risk_limit_store
        from app.signals.sizing import apply_fund_sizing
        from app.state import market_state

        ltp = market_state.ticks.get(latest.signal.token, {}).get("last_price")
        latest.signal.live_premium = float(ltp) if ltp and ltp > 0 else None
        # The affordability prefill must track the SAME premium the user sees:
        # a card issued at ₹100 whose contract now trades ₹130 buys fewer lots,
        # and prefilling yesterday's count would oversubmit the Kite basket.
        apply_fund_sizing(
            latest.signal,
            risk_limit_store.effective(get_settings()).get("trading_fund", 0.0),
        )
    return latest


@router.get("/{symbol}/history", response_model=list[SignalCard])
def signal_history(symbol: str, mode: str = Query("intraday")) -> list[SignalCard]:
    from app.signals.store import signal_store

    tmode = _resolve(symbol, mode)
    return signal_store.history(symbol, tmode)


class RepriceResult(BaseModel):
    status: str                       # "repriced" | "closed"
    signal: Optional[SignalCard] = None
    score: Optional[float] = None     # live score for the card's direction
    score_needed: Optional[float] = None
    reason: Optional[str] = None


@router.post("/{symbol}/reprice", response_model=RepriceResult)
def reprice_signal(symbol: str, mode: str = Query("intraday")) -> RepriceResult:
    """Re-validate the active card, then either re-price it or close it.

    Two things happen in one click, because a positional card that has sat for
    hours can be stale in BOTH senses:
      * its ENTRY PRICE is stale — the Kite hand-off refuses anything older than
        15 min because the zone was priced off a since-moved premium;
      * its THESIS may be stale — the live score for that direction may have
        decayed below the tradeable threshold while the card was held steady.

    So refresh reads the LIVE score for the card's direction (which the engine
    recomputes every cycle, even while the signal is held). If it no longer
    qualifies, the signal is CLOSED — handing back a freshly-priced card for a
    dead setup is the exact trap this guards against. If it still qualifies, the
    premium ladder is recomputed from the live LTP of the same strike and the
    card is re-stamped so the hand-off accepts it.
    """
    import time

    from app.signals import risk as risk_mod
    from app.signals.models import Direction
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

    profile = build_profiles(cfg).get(mode)
    if profile is None:
        raise HTTPException(status_code=409, detail=f"Mode '{mode}' is not enabled")

    # The engine can't score during warm-up, so a low number there is "no data",
    # not "thesis died" — do not close on it. Ask the user to retry shortly.
    if resp.status.regime == "warming_up":
        raise HTTPException(
            status_code=409,
            detail="Engine warming up — can't re-validate the score yet, try again shortly",
        )

    # Live score for THIS card's direction, recomputed this cycle.
    live_score = resp.status.bull_score if card.direction is Direction.CE else resp.status.bear_score
    now = int(time.time())

    if live_score < profile.score_valid:
        reason = (f"{card.direction.value} score is now {live_score:.0f}, "
                  f"below {profile.score_valid} — setup no longer valid, signal closed")
        signal_store.close_active(symbol, tmode, now, reason)
        return RepriceResult(status="closed", score=live_score,
                             score_needed=profile.score_valid, reason=reason)

    # Still valid → re-price against the live premium of the same strike.
    ltp = market_state.ticks.get(card.token, {}).get("last_price")
    if not ltp or ltp <= 0:
        raise HTTPException(
            status_code=409,
            detail="No live premium for this strike right now — cannot re-price",
        )
    disaster_pct = (cfg.premium_disaster_pct
                    if cfg.stop_primary == "underlying" and cfg.trading_capital > 0 else None)
    ladder = risk_mod.price_ladder(
        float(ltp), profile.premium_sl_pct, profile.rr_target1, profile.rr_target2,
        disaster_pct=disaster_pct, quick_pct=cfg.quick_target_pct or None,
    )
    refreshed = signal_store.reprice_active(symbol, tmode, float(ltp), ladder, now)
    if refreshed is None:
        raise HTTPException(status_code=409, detail="No active signal to refresh")
    return RepriceResult(status="repriced", signal=refreshed,
                         score=live_score, score_needed=profile.score_valid)
