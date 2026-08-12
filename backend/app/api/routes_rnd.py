"""R&D tab API — read-only window analytics over the paper book.

Nothing here mutates anything: it is a research surface. Policy ledgers (R3)
will add their endpoint when they exist; suggestions only ever come from a
ledger that cleared its bar, never from this analytics layer.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.config import get_settings
from app.rnd import analytics

router = APIRouter(prefix="/rnd", tags=["rnd"])


@router.get("/summary")
def rnd_summary() -> dict:
    return analytics.summary(get_settings())


@router.get("/ledger")
def rnd_ledger(limit: int = Query(200, ge=1, le=1000)) -> dict:
    rows, total = analytics.ledger(get_settings(), limit)
    return {"rows": rows, "count": len(rows), "total": total}
