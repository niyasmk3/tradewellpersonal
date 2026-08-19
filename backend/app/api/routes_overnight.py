"""Overnight tab endpoints — read-only research surface, same shape as /closing.

Separate router and separate stored results by design: re-running one tab's
analysis must never rewrite the other's evidence.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.kite.client import kite_service
from app.overnight.service import (
    OvernightError,
    load_results,
    run_analysis,
    run_sync,
    status,
)

log = logging.getLogger("tradewell.overnight")

router = APIRouter(prefix="/overnight", tags=["overnight"])
# THE SAME lock as /closing, not a sibling: overnight's sync delegates to
# closing's (same SQLite files), and its analyze reads the same stores —
# separate locks made the two tabs' 409 guards blind to each other
# (review catch).
from app.api.routes_closing import _lock  # noqa: E402


@router.get("/status")
async def get_status() -> dict:
    return status()


@router.post("/sync")
async def sync(years: int = Query(default=3, ge=1, le=5)) -> dict:
    if not kite_service.is_authenticated:
        raise HTTPException(status_code=401, detail="Kite login required")
    if _lock.locked():
        raise HTTPException(status_code=409, detail="An overnight job is already running")
    async with _lock:
        from app.api.routes_patterns import _lock as _patterns_lock
        async with _patterns_lock:
            try:
                return await asyncio.to_thread(run_sync, kite_service.kite, years)
            except OvernightError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            except Exception as exc:  # pragma: no cover - unexpected
                log.exception("Overnight sync failed")
                raise HTTPException(status_code=500, detail=f"Sync error: {exc}")


@router.post("/analyze")
async def analyze(lots: Optional[int] = Query(default=None, ge=1, le=100)) -> dict:
    if _lock.locked():
        raise HTTPException(status_code=409, detail="An overnight job is already running")
    async with _lock:
        try:
            n = lots if lots is not None else get_settings().closing_lots
            return await asyncio.to_thread(run_analysis, n)
        except OvernightError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("Overnight analysis failed")
            raise HTTPException(status_code=500, detail=f"Analysis error: {exc}")


@router.get("/results")
async def results() -> dict:
    data = load_results()
    if data is None:
        raise HTTPException(status_code=404,
                            detail="No results yet — POST /overnight/analyze")
    return data


@router.get("/trades")
async def trades(limit: int = Query(default=400, ge=1, le=2000),
                 which: str = Query(default="traded")) -> dict:
    """Ledgers, newest first. `which=traded` is the strategy; `which=skipped`
    is the disagreement nights it stood aside from — served so the tab can
    show what the filter removed, not just what it kept."""
    data = load_results()
    if data is None:
        raise HTTPException(status_code=404,
                            detail="No results yet — POST /overnight/analyze")
    key = "trades" if which == "traded" else "skipped_trades"
    rows = list(reversed((data.get("primary") or {}).get(key) or []))
    return {"rows": rows[:limit], "count": min(len(rows), limit),
            "total": len(rows), "which": which}
