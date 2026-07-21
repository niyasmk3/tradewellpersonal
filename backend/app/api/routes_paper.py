"""Paper-trading results — the simulated book, for post-market analysis."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.paper.service import summarize
from app.services import feed

router = APIRouter(prefix="/paper", tags=["paper"])


@router.get("/summary")
def paper_summary() -> dict:
    cfg = get_settings()
    store = getattr(feed, "paper_store", None)
    if not cfg.paper_trading or store is None:
        raise HTTPException(
            status_code=409,
            detail="Paper trading is off — set PAPER_TRADING=true and restart the backend",
        )
    out = summarize(store)
    out["slippage_pct"] = cfg.paper_slippage_pct
    out["lots"] = cfg.paper_lots
    return out
