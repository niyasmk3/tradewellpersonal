"""Runtime risk-limit settings — read and edit the loss guards from the UI.

Only the four loss/breaker limits are exposed. Everything else in config stays
.env-only on purpose: pausing the engine after a set loss is a safety valve;
changing a score threshold or stop percentage mid-session is a footgun.
"""
from __future__ import annotations

import logging

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.signals.risk_limits import risk_limit_store

log = logging.getLogger("tradewell.settings")

router = APIRouter(prefix="/settings", tags=["settings"])


class RiskLimitUpdate(BaseModel):
    # Every field optional: the UI PUTs only what changed. A model keeps the
    # keys constrained so an arbitrary config field can't be written here.
    daily_loss_limit: Optional[float] = Field(default=None, ge=0)
    max_open_drawdown: Optional[float] = Field(default=None, ge=0)
    max_consecutive_losses: Optional[int] = Field(default=None, ge=1, le=20)
    max_open_positions: Optional[int] = Field(default=None, ge=0, le=20)


@router.get("/risk-limits")
def get_risk_limits() -> dict:
    """Current value, source (.env or override), bounds, and how each works."""
    return risk_limit_store.view(get_settings())


@router.put("/risk-limits")
def set_risk_limits(body: RiskLimitUpdate) -> dict:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No values provided")
    try:
        result = risk_limit_store.set_many(updates, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("risk limits updated via UI: %s", updates)
    return result
