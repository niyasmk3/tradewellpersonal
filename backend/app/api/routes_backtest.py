"""Backtest REST endpoint (Phase 5).

Replays the signal engine over Kite historical data. The run is CPU-bound and
does blocking network I/O, so it's offloaded off the event loop.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from app.backtest.models import BacktestRequest, BacktestResult
from app.backtest.service import BacktestError, run_backtest

log = logging.getLogger("tradewell.backtest")

router = APIRouter(prefix="/backtest", tags=["backtest"])

# Single-flight: a run does chunked Kite historical pulls + a CPU-bound replay.
# A double-click must not launch parallel runs that hammer the historical API.
_run_lock = asyncio.Lock()


@router.post("", response_model=BacktestResult)
async def run(req: BacktestRequest) -> BacktestResult:
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="A backtest is already running — wait for it to finish")
    async with _run_lock:
        try:
            return await asyncio.to_thread(run_backtest, req)
        except BacktestError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("Backtest failed")
            raise HTTPException(status_code=500, detail=f"Backtest error: {exc}")
