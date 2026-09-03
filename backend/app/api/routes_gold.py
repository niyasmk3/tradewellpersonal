"""Gold module REST endpoints.

Sync (chunked Kite pulls), the XAUUSD backfill (up to ~940 curl fetches) and
the analysis (pandas crunching) are blocking, so all are offloaded off the
event loop; ONE single-flight lock covers them all because they share the two
gold SQLite stores. The lock is this module's own — the gold stores are
independent of closing/patterns, so their locks stay strangers on purpose.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from app.gold import store
from app.gold.service import (
    GoldError,
    live_cards,
    run_analysis,
    run_sync,
    run_xau_sync,
    status,
)
from app.kite.client import kite_service

log = logging.getLogger("tradewell.gold")

router = APIRouter(prefix="/gold", tags=["gold"])
_lock = asyncio.Lock()


@router.get("/status")
async def get_status() -> dict:
    return status()


@router.post("/sync")
async def sync() -> dict:
    if not kite_service.is_authenticated:
        raise HTTPException(status_code=401, detail="Kite login required")
    if _lock.locked():
        raise HTTPException(status_code=409, detail="A gold job is already running")
    async with _lock:
        try:
            return await asyncio.to_thread(run_sync, kite_service.kite)
        except GoldError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("Gold MCX sync failed")
            raise HTTPException(status_code=500, detail=f"Sync error: {exc}")


@router.post("/sync-xau")
async def sync_xau(years: int = Query(default=3, ge=1, le=5)) -> dict:
    """Dukascopy backfill — public data, no Kite login needed. Resume-safe, so
    a re-click after a partial run only fetches what is still missing."""
    if _lock.locked():
        raise HTTPException(status_code=409, detail="A gold job is already running")
    async with _lock:
        try:
            return await asyncio.to_thread(run_xau_sync, years)
        except Exception as exc:  # pragma: no cover - network
            log.exception("XAUUSD backfill failed")
            raise HTTPException(status_code=500, detail=f"Backfill error: {exc}")


@router.post("/analyze")
async def analyze() -> dict:
    if _lock.locked():
        raise HTTPException(status_code=409, detail="A gold job is already running")
    async with _lock:
        try:
            return await asyncio.to_thread(run_analysis)
        except GoldError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("Gold analysis failed")
            raise HTTPException(status_code=500, detail=f"Analysis error: {exc}")


@router.get("/results")
async def results() -> dict:
    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404, detail="No results yet — POST /gold/analyze")
    return data


@router.get("/trades")
async def trades(rule: str = Query(default="all"),
                 phase: str = Query(default="all"),
                 limit: int = Query(default=400, ge=1, le=2000)) -> dict:
    """The closed-trade ledger, newest first. Backtest and forward rows carry
    their phase — display them separately, always (spec §5)."""
    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404, detail="No results yet — POST /gold/analyze")
    rows = list(reversed(data.get("trades") or []))
    if rule != "all":
        rows = [r for r in rows if r.get("rule") == rule]
    if phase != "all":
        rows = [r for r in rows if r.get("phase") == phase]
    return {"rows": rows[:limit], "count": min(len(rows), limit),
            "total": len(rows), "rule": rule, "phase": phase}


@router.get("/live")
async def live() -> dict:
    """Today's tape through the one code path, live=True — the same cards the
    alert loop watches, computed on demand for the tab."""
    if not kite_service.is_authenticated:
        raise HTTPException(status_code=401, detail="Kite login required")
    try:
        return await asyncio.to_thread(live_cards, kite_service.kite)
    except Exception as exc:  # pragma: no cover - Kite/network
        log.exception("Gold live read failed")
        raise HTTPException(status_code=500, detail=f"Live read error: {exc}")
