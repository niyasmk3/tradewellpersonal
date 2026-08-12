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


@router.get("/policies")
def rnd_policies() -> dict:
    """State of the three pre-registered exit-policy ledgers (R3)."""
    from app.rnd import policies
    from app.services import feed

    cfg = get_settings()
    store = getattr(feed, "paper_store", None)
    if not cfg.rnd_policy_ledgers or store is None:
        return {"enabled": False, "min_diverged": policies.MIN_DIVERGED,
                "policies": {}}
    out = policies.policy_summary(store, cfg)
    out["enabled"] = True
    return out


@router.get("/candidates")
def rnd_candidates() -> dict:
    """Deterministic insight scanner (R3) — candidates, never suggestions."""
    from app.rnd import scanner
    return scanner.scan(get_settings())
