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
from fastapi.responses import Response

from app.closing import store
from app.closing.service import ClosingError, run_analysis, run_sync, run_tonight, status
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


@router.get("/tonight")
async def tonight() -> dict:
    """Today's 15:00 pre-trade card: live values + the five registered risk
    checks. Light Kite fetch, in-memory only — no store writes, so it skips
    the sync/analyze single-flight lock."""
    if not kite_service.is_authenticated:
        raise HTTPException(status_code=401, detail="Kite login required")
    try:
        return await asyncio.to_thread(run_tonight, kite_service.kite)
    except ClosingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # pragma: no cover - unexpected
        log.exception("Tonight read failed")
        raise HTTPException(status_code=500, detail=f"Tonight error: {exc}")


@router.get("/snapshots")
async def get_snapshots(limit: int = Query(default=30, ge=1, le=400)) -> dict:
    """The 15:00 research log (GIFT prints + chain skew), newest first.
    Pre-registered 2026-08-22; rows accumulate toward the ~150-200 night
    verdict horizon. Pure file read — no locks, no Kite."""
    from app.closing import snapshot

    rows = snapshot.snapshots(limit)
    return {"rows": rows, "count": len(rows),
            "registered_on": snapshot.REGISTERED_ON}


@router.post("/snapshot")
async def take_snapshot() -> dict:
    """Manual capture of today's row (idempotent — the first full row per
    date stands). The backend loop normally does this at 15:05; this exists
    for catch-up when the server was down over the window. GIFT logs without
    Kite; the chain block needs the login."""
    from app.closing import snapshot

    kite = kite_service.kite if kite_service.is_authenticated else None
    return await asyncio.to_thread(snapshot.run_snapshot, kite)


@router.get("/results")
async def results() -> dict:
    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404,
                            detail="No results yet — POST /closing/analyze")
    return data


# Leading columns in reading order; every other field the ledger row carries
# follows alphabetically, so nothing stamped on a row is lost in the export.
_LEDGER_LEAD = [
    "date", "exit_date", "direction", "card_verdict", "tier", "card_red",
    "day_open", "p1400", "signal_price", "gap_pts", "entry_spot", "exit_spot",
    "signed_move_pts", "strike", "expiry", "dte_entry", "vix_in", "vix_out",
    "mid_in", "mid_out", "fill_in", "fill_out", "qty", "gross_rs", "charges_rs",
    "net_rs", "net_pct", "loss_reason", "cpr_width_pct", "f_cpr_narrow",
    "x1045_exit_spot", "x1045_fill_out", "x1045_net_rs", "x1045_net_pct",
    "x1045_delta_rs",
]


@router.get("/trades.xlsx")
async def trades_xlsx() -> Response:
    """The primary window's full ledger as a real .xlsx (stdlib writer, no
    openpyxl). Oldest night first, header row frozen."""
    from app.closing import xlsx

    data = store.load_results()
    if data is None:
        raise HTTPException(status_code=404,
                            detail="No results yet — POST /closing/analyze")
    rows = (data.get("primary") or {}).get("trades") or []
    seen = {k for r in rows for k in r}
    columns = [c for c in _LEDGER_LEAD if c in seen] + sorted(seen - set(_LEDGER_LEAD))
    stamp = (data.get("primary") or {}).get("to") or "ledger"
    body = xlsx.workbook(rows, columns, sheet="Closing ledger")
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="closing_ledger_{stamp}.xlsx"'},
    )


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
