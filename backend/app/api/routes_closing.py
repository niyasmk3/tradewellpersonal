"""Closing Day Strategy endpoints — a read-only research surface.

Nothing here places an order, emits a signal or mutates engine state; the
module's whole output is a stored analysis. Sync and analysis are blocking
(chunked Kite pulls, then a full-history backtest), so both are offloaded off
the event loop behind a single-flight lock, the same shape as /patterns.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.closing import store
from app.closing.service import ClosingError, run_analysis, run_sync, status
from app.config import get_settings
from app.kite.client import kite_service

log = logging.getLogger("tradewell.closing")

router = APIRouter(prefix="/closing", tags=["closing"])
# Shared with routes_overnight: both tabs' jobs read/write the same stores
# (.closing_vix.db + the patterns spine + the pricing model inputs), so their
# single-flight guards must be the SAME lock — two per-router locks let two
# "Sync + Analyze" clicks upsert the same SQLite tables concurrently
# (review catch).
_lock = asyncio.Lock()


@router.get("/status")
async def get_status() -> dict:
    return status()


@router.post("/sync")
async def sync(years: int = Query(default=3, ge=1, le=5)) -> dict:
    if not kite_service.is_authenticated:
        raise HTTPException(status_code=401, detail="Kite login required")
    if _lock.locked():
        raise HTTPException(status_code=409, detail="A closing job is already running")
    async with _lock:
        # The spine refresh runs through patterns' own sync, so take that
        # module's lock too — otherwise /patterns/sync can write the same
        # candle table mid-pull (lock order everywhere: closing -> patterns).
        from app.api.routes_patterns import _lock as _patterns_lock
        async with _patterns_lock:
            try:
                return await asyncio.to_thread(run_sync, kite_service.kite, years)
            except ClosingError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            except Exception as exc:  # pragma: no cover - unexpected
                log.exception("Closing sync failed")
                raise HTTPException(status_code=500, detail=f"Sync error: {exc}")


@router.post("/analyze")
async def analyze(lots: Optional[int] = Query(default=None, ge=1, le=100)) -> dict:
    if _lock.locked():
        raise HTTPException(status_code=409, detail="A closing job is already running")
    async with _lock:
        try:
            n = lots if lots is not None else get_settings().closing_lots
            return await asyncio.to_thread(run_analysis, n)
        except ClosingError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("Closing analysis failed")
            raise HTTPException(status_code=500, detail=f"Analysis error: {exc}")


@router.get("/results")
async def results() -> dict:
    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404,
                            detail="No results yet — POST /closing/analyze")
    return data


@router.get("/trades")
async def trades(limit: int = Query(default=400, ge=1, le=2000)) -> dict:
    """The primary window's trade ledger, newest first.

    Served apart from /results because the ledger is the bulk of the payload
    and the summary cards do not need it.
    """
    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404,
                            detail="No results yet — POST /closing/analyze")
    rows = list(reversed((data.get("primary") or {}).get("trades") or []))
    return {"rows": rows[:limit], "count": min(len(rows), limit), "total": len(rows)}
