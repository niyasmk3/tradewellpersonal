"""Strategy Lab REST endpoints.

Backtests and syncs are blocking pandas/sqlite work, so they run off the
event loop behind ONE single-flight lock (they share the lab stores). Reads
are cheap sqlite queries and stay unlocked.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.lab import risk, service
from app.lab.service import LabError
from app.lab.store import lab_store

log = logging.getLogger("tradewell.lab")

router = APIRouter(prefix="/lab", tags=["lab"])
_lock = asyncio.Lock()


class BacktestRequest(BaseModel):
    sid: str
    version: Optional[int] = None
    params: Optional[dict] = None
    from_day: Optional[str] = Field(default=None,
                                    pattern=r"^\d{4}-\d{2}-\d{2}$")
    to_day: Optional[str] = Field(default=None,
                                  pattern=r"^\d{4}-\d{2}-\d{2}$")
    capital_rs: float = Field(default=500000.0, ge=10000, le=100000000)
    kind: str = Field(default="insample",
                      pattern=r"^(insample|oos|walkforward)$")


class VersionRequest(BaseModel):
    params: dict
    note: str = ""


class PaperToggle(BaseModel):
    enabled: bool


class ChampionRequest(BaseModel):
    version: int


class RiskUpdate(BaseModel):
    updates: dict


class EmergencyRequest(BaseModel):
    action: str = Field(pattern=r"^(trip|clear)$")
    flatten: bool = False
    reason: str = ""


@router.get("/overview")
async def overview() -> dict:
    return await asyncio.to_thread(service.overview)


@router.get("/library")
async def library() -> list:
    return await asyncio.to_thread(service.library)


@router.get("/strategy/{sid}")
async def strategy_detail(sid: str) -> dict:
    try:
        return await asyncio.to_thread(service.strategy_detail, sid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/strategy/{sid}/version")
async def create_version(sid: str, req: VersionRequest) -> dict:
    try:
        return await asyncio.to_thread(service.save_version, sid,
                                       req.params, req.note)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/strategy/{sid}/champion")
async def set_champion(sid: str, req: ChampionRequest) -> dict:
    try:
        return await asyncio.to_thread(service.set_champion, sid,
                                       req.version)
    except (KeyError, LabError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/strategy/{sid}/paper")
async def toggle_paper(sid: str, req: PaperToggle) -> dict:
    try:
        return await asyncio.to_thread(service.set_paper, sid, req.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/backtest")
async def backtest(req: BacktestRequest) -> dict:
    if _lock.locked():
        raise HTTPException(status_code=409,
                            detail="A lab job is already running")
    async with _lock:
        try:
            return await asyncio.to_thread(
                service.run_backtest, req.sid, req.version, req.params,
                req.from_day, req.to_day, req.capital_rs, req.kind)
        except (KeyError, ValueError, LabError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("Lab backtest failed")
            raise HTTPException(status_code=500,
                                detail="Backtest error: %s" % exc)


@router.get("/runs")
async def runs(sid: Optional[str] = Query(default=None),
               limit: int = Query(default=50, ge=1, le=200)) -> list:
    return await asyncio.to_thread(lab_store.runs, sid, limit)


@router.get("/runs/{run_id}")
async def run_detail(run_id: int,
                     trades: int = Query(default=200, ge=0, le=2000)
                     ) -> dict:
    run = await asyncio.to_thread(lab_store.run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    run["trades"] = await asyncio.to_thread(lab_store.run_trades, run_id,
                                            trades)
    return run


@router.get("/paper")
async def paper() -> dict:
    return await asyncio.to_thread(service.paper_status)


@router.get("/paper/days")
async def paper_days(sid: Optional[str] = Query(default=None),
                     limit: int = Query(default=120, ge=1, le=1000)) -> list:
    return await asyncio.to_thread(lab_store.paper_days, sid, limit)


@router.get("/experiments")
async def experiments(sid: Optional[str] = Query(default=None),
                      limit: int = Query(default=100, ge=1, le=200)) -> list:
    return await asyncio.to_thread(lab_store.experiments, sid, limit)


@router.get("/report")
async def latest_report() -> dict:
    days = await asyncio.to_thread(lab_store.report_days, 1)
    if not days:
        raise HTTPException(status_code=404,
                            detail="No daily report yet — one is written "
                                   "after each paper session")
    rep = await asyncio.to_thread(lab_store.report, days[0])
    return rep or {}


@router.get("/report/{day}")
async def report_for(day: str) -> dict:
    rep = await asyncio.to_thread(lab_store.report, day)
    if rep is None:
        raise HTTPException(status_code=404, detail="no report for %s" % day)
    return rep


@router.get("/reports")
async def report_list(limit: int = Query(default=30, ge=1, le=120)) -> list:
    return await asyncio.to_thread(lab_store.report_days, limit)


@router.get("/risk")
async def risk_view() -> dict:
    return {"limits": risk.effective(),
            "events": lab_store.risk_events(limit=50),
            "bounds": {k: {"default": v[0], "lo": v[1], "hi": v[2]}
                       for k, v in risk.LIMITS.items()},
            "note": "margins are estimates; simulated portfolio only"}


@router.post("/risk")
async def risk_set(req: RiskUpdate) -> dict:
    try:
        return {"limits": await asyncio.to_thread(risk.set_limits,
                                                  req.updates)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/emergency")
async def emergency(req: EmergencyRequest) -> dict:
    if req.action == "trip":
        await asyncio.to_thread(risk.trip_emergency,
                                req.reason or "manual", req.flatten)
    else:
        await asyncio.to_thread(risk.clear_emergency)
    return {"limits": risk.effective()}


@router.get("/data")
async def data_status() -> dict:
    return await asyncio.to_thread(service.data_status)


@router.post("/data/sync-futures")
async def sync_futures() -> dict:
    if _lock.locked():
        raise HTTPException(status_code=409,
                            detail="A lab job is already running")
    async with _lock:
        try:
            return await asyncio.to_thread(service.sync_futures_now)
        except LabError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:  # pragma: no cover - Kite/network
            log.exception("Lab futures sync failed")
            raise HTTPException(status_code=500,
                                detail="Sync error: %s" % exc)
