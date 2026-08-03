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


@router.get("/{symbol}/score-history")
def score_history_series(
    symbol: str,
    mode: str = Query("intraday"),
    minutes: int = Query(120, ge=1, le=480),
) -> dict:
    """Rolling tail of the directional scores (see signals/score_history.py).

    One point per evaluation (~5s), decimated server-side; same-day only. The
    UI draws component sparklines from this when a score row is hovered.
    """
    from app.signals.score_history import score_history

    tmode = _resolve(symbol, mode)
    return score_history.series(symbol.upper(), tmode.value, minutes)


@router.get("/{symbol}/history")
def signal_history(symbol: str, mode: str = Query("all")) -> dict:
    """Every card the engine has issued lately — including the ones you missed.

    The store already keeps the last 50 cards per mode with their retirement
    state; this joins them against the trade journal and the paper book so each
    row says not just what happened to the CARD (expired/cancelled/invalidated)
    but whether YOU acted on it. `state` is effective, not stored: a card
    persisted "active" whose validity has passed reads as expired — the stored
    flag is only updated on the next reconcile, and this view must not show a
    dead card as live.
    """
    import time as _time

    from app.signals.store import signal_store
    from app.trades.store import trade_store

    cfg = get_settings()
    if symbol.upper() not in cfg.signal_symbols:
        raise HTTPException(status_code=404, detail=f"Signals not enabled for {symbol}")
    modes = cfg.signal_mode_list if mode == "all" else [_resolve(symbol, mode).value]

    live_ids = {t.signal_id for t in trade_store.all() if t.signal_id}
    paper_ids: set[str] = set()
    try:
        from app.services import feed

        if getattr(feed, "paper_store", None) is not None:
            paper_ids = {t.signal_id for t in feed.paper_store.all() if t.signal_id}
    except Exception:
        pass  # paper trading off — the column just stays empty

    now = int(_time.time())
    rows = []
    for m in modes:
        try:
            cards = signal_store.history(symbol.upper(), TradingMode(m))
        except ValueError:
            continue
        for c in cards:
            state = c.state.value
            if state == "active" and now >= c.valid_until:
                state = "expired"
            taken = ("both" if c.id in live_ids and c.id in paper_ids
                     else "live" if c.id in live_ids
                     else "paper" if c.id in paper_ids
                     else None)
            rows.append({
                "id": c.id, "mode": m, "direction": c.direction.value,
                "contract": c.contract, "score": c.confidence, "title": c.title,
                "state": state, "taken": taken,
                "created_at": c.created_at, "valid_until": c.valid_until,
                "entry_low": c.entry_low, "entry_high": c.entry_high,
                "premium_sl": c.premium_sl, "target1": c.target1, "target2": c.target2,
                "ref_entry_premium": c.ref_entry_premium,
            })
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"rows": rows, "count": len(rows)}


@router.get("/{symbol}/archive")
def signal_archive_history(symbol: str, mode: str = Query("all"),
                           days: int = Query(7, ge=1, le=90)) -> dict:
    """Multi-day card history from the append-only archive.

    The in-memory store is deliberately day-scoped (yesterday's zones must
    never serve as live), so this reads signals/archive.py instead — every
    adoption and retirement since the archive shipped, plus whatever the
    boot-merge harvested. Today's cards are merged from the LIVE store by id
    (fresher state wins), and the same effective-state / taken-join rules as
    /history apply, so the tab renders both sources identically.
    """
    import time as _time

    from app.signals.archive import signal_archive
    from app.signals.store import signal_store
    from app.trades.store import trade_store

    cfg = get_settings()
    if symbol.upper() not in cfg.signal_symbols:
        raise HTTPException(status_code=404, detail=f"Signals not enabled for {symbol}")
    modes = cfg.signal_mode_list if mode == "all" else [_resolve(symbol, mode).value]

    cards = {c.id: c for c in signal_archive.load(days=days, symbol=symbol.upper(),
                                                  modes=modes)}
    for m in modes:                       # live store wins: freshest state
        try:
            for c in signal_store.history(symbol.upper(), TradingMode(m)):
                cards[c.id] = c
        except ValueError:
            continue

    live_ids = {t.signal_id for t in trade_store.all() if t.signal_id}
    paper_ids: set[str] = set()
    try:
        from app.services import feed

        if getattr(feed, "paper_store", None) is not None:
            paper_ids = {t.signal_id for t in feed.paper_store.all() if t.signal_id}
    except Exception:
        pass                              # paper trading off — column stays empty

    now = int(_time.time())
    rows = []
    for c in cards.values():
        state = c.state.value
        if state == "active" and now >= c.valid_until:
            state = "expired"
        taken = ("both" if c.id in live_ids and c.id in paper_ids
                 else "live" if c.id in live_ids
                 else "paper" if c.id in paper_ids
                 else None)
        rows.append({
            "id": c.id, "mode": c.mode.value, "direction": c.direction.value,
            "contract": c.contract, "score": c.confidence, "title": c.title,
            "state": state, "taken": taken,
            "created_at": c.created_at, "valid_until": c.valid_until,
            "entry_low": c.entry_low, "entry_high": c.entry_high,
            "premium_sl": c.premium_sl, "target1": c.target1, "target2": c.target2,
            "ref_entry_premium": c.ref_entry_premium,
        })
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"rows": rows, "count": len(rows), "days": days}


@router.get("/{symbol}/eval-trace")
def signal_eval_trace(symbol: str, mode: str = Query("all"),
                      days: int = Query(1, ge=1, le=90)) -> dict:
    """The per-bar evaluation trace (audit P1-2) — every bar's bull/bear
    scores, regime vote and veto, whether or not a card was offered. This is
    the raw dataset for classifying missed moves; it has no UI, it exists so
    the next audit reads measurements instead of reconstructing hindsight.
    """
    from app.signals.eval_trace import eval_trace

    cfg = get_settings()
    if symbol.upper() not in cfg.signal_symbols:
        raise HTTPException(status_code=404, detail=f"Signals not enabled for {symbol}")
    m = None if mode == "all" else _resolve(symbol, mode).value
    rows = eval_trace.load(days=days, symbol=symbol.upper(), mode=m)
    return {"rows": rows, "count": len(rows), "days": days,
            "retention_days": cfg.eval_trace_days}


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
        import time as _time

        from app.config import get_settings
        from app.signals.risk_limits import risk_limit_store
        from app.signals.sizing import apply_fund_sizing
        from app.state import market_state

        latest.signal.live_premium = _fresh_ltp(
            market_state.ticks, latest.signal.token, get_settings(), int(_time.time())
        )
        # The affordability prefill must track the SAME premium the user sees:
        # a card issued at ₹100 whose contract now trades ₹130 buys fewer lots,
        # and prefilling yesterday's count would oversubmit the Kite basket.
        apply_fund_sizing(
            latest.signal,
            risk_limit_store.effective(get_settings()).get("trading_fund", 0.0),
        )
    return latest


def _fresh_ltp(ticks: dict, token: int, cfg, now: int) -> float | None:
    """The token's live premium, or None when its quote fails the age gate.

    The tick dict is never evicted, so a dead stream leaves last_price frozen
    at its final trade indefinitely — "live_premium" from it would be a lie
    with decimals. Same cutoff as issuance (signal_max_premium_age_s); a tick
    with no exchange stamp is unverifiable and treated as stale. Gate disabled
    (0) keeps the old behaviour for offline replay.
    """
    tick = ticks.get(token) or {}
    ltp = tick.get("last_price")
    if not ltp or ltp <= 0:
        return None
    max_age = cfg.signal_max_premium_age_s
    if max_age > 0:
        ts = tick.get("ts")
        if not ts or now - int(ts) > max_age:
            return None
    return float(ltp)


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
    # SAME freshness rule as issuance. Repricing exists to REMOVE staleness,
    # and reprice_active re-stamps created_at so the Kite hand-off's age guard
    # passes — so a stale tick accepted here would be laundered into a
    # "fresh" card with one click, recreating the exact unfillable-zone bug
    # the issue-time gate closed. A frozen tick stream must 409, not re-price.
    ltp = _fresh_ltp(market_state.ticks, card.token, cfg, now)
    if not ltp:
        raise HTTPException(
            status_code=409,
            detail="No fresh premium for this strike right now — cannot re-price",
        )
    disaster_pct = (cfg.premium_disaster_pct
                    if cfg.stop_primary == "underlying" and cfg.trading_capital > 0 else None)
    # The SAME calibrated rr1 the card was issued with — a refresh must not
    # silently revert an excursion-derived T1 back to the static +27%.
    from app.signals import calibration

    rr1 = calibration.intraday_rr1(profile, cfg) or profile.rr_target1
    ladder = risk_mod.price_ladder(
        float(ltp), profile.premium_sl_pct, rr1, profile.rr_target2,
        disaster_pct=disaster_pct, quick_pct=cfg.quick_target_pct or None,
    )
    # Best-effort: refreshing ref_spot fixes the stale-spot mismatch that
    # caused a hair-trigger invalidation, but its absence must never fail the
    # premium re-price itself.
    try:
        snap = market_state.underlying_snapshot(symbol)
        spot = snap.ltp if snap else None
    except Exception:
        spot = None
    refreshed = signal_store.reprice_active(
        symbol, tmode, float(ltp), ladder, now, spot=spot)
    if refreshed is None:
        raise HTTPException(status_code=409, detail="No active signal to refresh")
    return RepriceResult(status="repriced", signal=refreshed,
                         score=live_score, score_needed=profile.score_valid)
