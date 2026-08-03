"""Patterns Module REST endpoints.

Sync + analysis are blocking (chunked Kite pulls, pandas crunching), so both
are offloaded off the event loop; a single-flight lock stops a double-click
from launching parallel Kite pulls (same pattern as the backtest route).
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Query

from app.kite.client import kite_service
from app.patterns import store
from app.patterns.levels import levels_near
from app.patterns.service import PatternsError, run_analysis, run_sync, status

log = logging.getLogger("tradewell.patterns")

router = APIRouter(prefix="/patterns", tags=["patterns"])
_lock = asyncio.Lock()


@router.get("/status")
async def get_status() -> dict:
    return status()


@router.post("/sync")
async def sync(years: int = Query(default=3, ge=1, le=5)) -> dict:
    if not kite_service.is_authenticated:
        raise HTTPException(status_code=401, detail="Kite login required")
    if _lock.locked():
        raise HTTPException(status_code=409, detail="A patterns job is already running")
    async with _lock:
        try:
            return await asyncio.to_thread(run_sync, kite_service.kite, years)
        except PatternsError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("Patterns sync failed")
            raise HTTPException(status_code=500, detail=f"Sync error: {exc}")


@router.post("/analyze")
async def analyze() -> dict:
    if _lock.locked():
        raise HTTPException(status_code=409, detail="A patterns job is already running")
    async with _lock:
        try:
            return await asyncio.to_thread(run_analysis)
        except PatternsError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("Patterns analysis failed")
            raise HTTPException(status_code=500, detail=f"Analysis error: {exc}")


@router.get("/results")
async def results() -> dict:
    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404, detail="No results yet — POST /patterns/analyze")
    return data


@router.get("/live-read")
async def live_read() -> dict:
    """Today's tape against the 3-year conditional frequencies: patterns on
    the last 30 minutes of closed 5-min bars (each joined to its
    volume-conditioned historical cell) plus the day's volume pace. Reads the
    stored analysis — POST /patterns/analyze after a sync to (re)build it.
    """
    if not kite_service.is_authenticated:
        raise HTTPException(status_code=401, detail="Kite login required")
    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404, detail="No results yet — POST /patterns/analyze")
    if "conditional_outcomes" not in data:
        raise HTTPException(
            status_code=409,
            detail="Results predate the tendencies layer — POST /patterns/analyze to rebuild")

    from app.patterns.tendencies import assemble_live_read, fetch_today

    def _read() -> dict:
        today = fetch_today(kite_service.kite)
        tail = store.load_tail(60)
        return assemble_live_read(today, tail, data)

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:  # pragma: no cover - Kite/network
        log.exception("Patterns live read failed")
        raise HTTPException(status_code=500, detail=f"Live read error: {exc}")


@router.get("/level-alerts")
async def level_alerts() -> dict:
    """Recent level-touch callouts + the levels currently on watch — the
    dashboard's chart overlay and alert banner read this. The watch itself
    lives in the feed loop; this only reports its state."""
    from app.services import feed

    lw = getattr(feed, "level_watch", None)
    if lw is None:
        return {"enabled": False, "watched": [], "alerts": []}
    from app.state import market_state

    snap = market_state.underlying_snapshot("NIFTY")
    spot = float(snap.ltp) if snap and snap.ltp else None
    lw.refresh_levels(time.time())
    return {
        "enabled": bool(getattr(lw.cfg, "level_alerts_enabled", True)),
        "spot": spot,
        "watched": lw.watched(spot),
        "alerts": lw.recent(),
    }


@router.get("/levels")
async def levels(
    near: float = Query(default=0, description="Filter to levels near this price (0 = current close)"),
    window_pct: float = Query(default=2.0, gt=0, le=20),
) -> dict:
    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404, detail="No results yet — POST /patterns/analyze")
    all_levels = data.get("levels", {}).get("levels", [])
    anchor = near or data.get("data", {}).get("last_close", 0)
    return {
        "anchor_price": anchor,
        "window_pct": window_pct,
        "levels": levels_near(all_levels, anchor, window_pct) if anchor else all_levels,
    }
