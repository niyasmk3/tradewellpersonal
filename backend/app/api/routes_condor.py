"""Iron Condor API. Advisory only — nothing here places, modifies or exits an
order; positions are journals of trades the user executed manually in Kite.

Review-hardened (11-Aug adversarial review): position views resolve each
position's OWN universe; journal entry validates structure geometry; what-if
never silently substitutes a different structure or a fabricated vol; the
adjustment cap is journaled via /positions/{pid}/adjust."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.condor.models import (
    AdjustCondorRequest,
    CondorResponse,
    EnterCondorRequest,
    ExitCondorRequest,
    WhatIfRequest,
)
from app.condor.monitor import build_position
from app.condor.store import condor_archive, condor_store, condor_trace
from app.config import get_settings

router = APIRouter(prefix="/condor", tags=["condor"])


def _svc():
    from app.services import feed
    svc = getattr(feed, "condor", None)
    if svc is None:
        raise HTTPException(
            status_code=409,
            detail="Condor module not running (CONDOR_ENABLED=false or feed stopped)")
    return svc


@router.get("/{symbol}", response_model=CondorResponse)
def condor_state(symbol: str) -> CondorResponse:
    svc = _svc()
    symbol = symbol.upper()
    if symbol not in get_settings().condor_symbol_list:
        raise HTTPException(status_code=404, detail=f"condor not enabled for {symbol}")
    raw = svc.last_responses.get(symbol)
    if raw is None:
        raise HTTPException(status_code=503, detail="no condor evaluation yet")
    resp = CondorResponse(**raw)
    # Serve the reconciled card (store owns lifecycle/birth timestamps).
    resp.card = condor_store.latest(symbol)
    if resp.card and resp.card.state != "active":
        resp.card = None
    return resp


@router.get("/{symbol}/history")
def condor_history(symbol: str, days: int = Query(7, ge=1, le=90)) -> dict:
    rows = [r for r in condor_archive.rows(days)
            if r.get("symbol", "").upper() == symbol.upper()]
    return {"rows": rows, "count": len(rows), "days": days}


@router.get("/{symbol}/trace")
def condor_eval_trace(symbol: str, days: int = Query(1, ge=1, le=30)) -> dict:
    rows = [r for r in condor_trace.rows(days)
            if r.get("symbol", "").upper() == symbol.upper()]
    return {"rows": rows, "count": len(rows), "days": days}


@router.get("/positions/all")
def positions() -> dict:
    svc = _svc()
    band = None
    for raw in svc.last_responses.values():
        band = raw.get("breakout_band") or band
    # Each position resolves its own symbol+expiry universe inside the
    # monitor; an unmatched position comes back with a DATA QUALITY view
    # instead of silently vanishing or being priced off the wrong week.
    views = svc.monitor.views(svc.universes, band)
    closed = [p.model_dump() for p in condor_store.all_positions() if p.status == "closed"]
    return {"open": [v.model_dump() for v in views], "closed": closed}


@router.post("/positions")
def enter_position(req: EnterCondorRequest) -> dict:
    svc = _svc()
    symbol = req.symbol.upper()
    uni = svc.universe_for(symbol)
    if uni is None:
        raise HTTPException(status_code=409, detail=f"no universe for {symbol}")
    from app.condor.engine import universe_lot
    lot = universe_lot(uni)
    if lot <= 0:
        raise HTTPException(status_code=409, detail="lot size unknown")
    # Structure geometry (review: inverted/asymmetric condors were journal-
    # able, and every downstream formula assumes ordered shorts + equal wings).
    if req.short_pe_strike >= req.short_ce_strike:
        raise HTTPException(status_code=422,
                            detail="short PE strike must be below short CE strike")
    if req.wing_ce_strike <= req.short_ce_strike or req.wing_pe_strike >= req.short_pe_strike:
        raise HTTPException(status_code=422,
                            detail="wings must be beyond shorts (CE above, PE below)")
    width_ce = req.wing_ce_strike - req.short_ce_strike
    width_pe = req.short_pe_strike - req.wing_pe_strike
    if abs(width_ce - width_pe) > 1e-6:
        raise HTTPException(
            status_code=422,
            detail=f"wing widths must match (CE {width_ce:.0f} vs PE {width_pe:.0f} pts) "
                   "— asymmetric structures aren't supported in v1")
    pos = build_position(req, lot, uni.expiry.isoformat() if uni.expiry else None)
    if pos.credit_fill <= 0:
        raise HTTPException(status_code=422, detail="net credit must be positive")
    condor_store.add_position(pos)
    return {"status": "recorded", "position": pos.model_dump()}


@router.post("/positions/{pid}/exit")
def exit_position(pid: str, req: ExitCondorRequest) -> dict:
    svc = _svc()
    pos = svc.monitor.close_position(pid, req.exit_debit, req.reason)
    if pos is None:
        raise HTTPException(status_code=404, detail="open position not found")
    return {"status": "closed", "position": pos.model_dump()}


@router.post("/positions/{pid}/adjust")
def adjust_position(pid: str, req: AdjustCondorRequest) -> dict:
    """Journal a manually-executed roll. This is what makes the adjustment
    cap real: the second ADJUST trigger on a rolled position recommends EXIT."""
    svc = _svc()
    pos = svc.monitor.record_adjustment(pid, req)
    if pos is None:
        raise HTTPException(status_code=404, detail="open position not found")
    return {"status": "adjusted", "position": pos.model_dump()}


@router.post("/whatif")
def whatif(req: WhatIfRequest) -> dict:
    svc = _svc()
    card = None
    if req.card_id:
        for sym in get_settings().condor_symbol_list:
            c = condor_store.latest(sym)
            if c and c.id == req.card_id and c.state == "active":
                card = c
                break
        if card is None:
            # Never silently price a DIFFERENT structure than the one the
            # user is looking at (review P3/C19).
            raise HTTPException(status_code=404,
                                detail="card no longer active — refresh and retry")
    elif req.position_id:
        pos = condor_store.get_position(req.position_id)
        if pos is None:
            raise HTTPException(status_code=404, detail="position not found")
        card = svc.position_pseudo_card(pos)
        if card is None:
            raise HTTPException(
                status_code=409,
                detail="cannot price scenarios — no live IVs for this position's "
                       "legs (expiry not subscribed or quotes stale)")
    else:
        syms = get_settings().condor_symbol_list
        card = condor_store.latest(syms[0]) if syms else None
        if card is None or card.state != "active":
            raise HTTPException(status_code=404, detail="no active card to analyze")
    try:
        return svc.whatif(card, req)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/config/view")
def config_view() -> dict:
    cfg = get_settings()
    return {
        "enabled": cfg.condor_enabled,
        "symbols": cfg.condor_symbol_list,
        "profile": cfg.condor_profile,
        "dte": [cfg.condor_min_dte, cfg.condor_max_dte],
        "score_min": cfg.condor_score_min,
        "watch_min": cfg.condor_watch_min,
        "min_credit_pct": cfg.condor_min_credit_pct,
        "min_em_dist": cfg.condor_min_em_dist,
        "wing_widths": cfg.condor_wing_width_list,
        "min_pop": cfg.condor_min_pop,
        "max_loss_per_trade": cfg.condor_max_loss_per_trade,
        "sl_mult": cfg.condor_sl_mult,
        "profit_target_pct": cfg.condor_profit_target_pct,
        "entry_window": [cfg.condor_entry_from, cfg.condor_entry_to],
        "lots": cfg.condor_lots,
        "note": "Advisory only. All thresholds are .env-configured (CONDOR_*); "
                "cards are TRIAL until the shadow ledger reads out at n>=30.",
    }
