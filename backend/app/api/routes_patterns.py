"""Patterns Module REST endpoints.

Sync + analysis are blocking (chunked Kite pulls, pandas crunching), so both
are offloaded off the event loop; a single-flight lock stops a double-click
from launching parallel Kite pulls (same pattern as the backtest route).
"""
from __future__ import annotations

import asyncio
import logging

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
