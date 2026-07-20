"""News-intelligence REST endpoint."""
from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.news.models import NewsResponse
from app.news.store import news_store

router = APIRouter(prefix="/news", tags=["news"])


@router.get("", response_model=NewsResponse)
def news() -> NewsResponse:
    cfg = get_settings()
    if not cfg.news_active:
        note = (
            "News analysis is off. Set ANTHROPIC_API_KEY (and NEWS_ENABLED=true) "
            "in backend/.env to enable Claude-powered sentiment."
        )
        return NewsResponse(enabled=False, note=note)

    sentiments = [news_store.sentiment(sym) for sym in cfg.signal_symbols]
    items = news_store.recent(limit=40)
    # Newest first so the dashboard alerts on the freshest breaking item.
    breaking = sorted(
        (i for i in items if i.is_market_moving and i.impact_score >= cfg.news_breaking_impact),
        key=lambda i: i.analyzed_at,
        reverse=True,
    )[:5]
    return NewsResponse(
        enabled=True,
        updated_at=news_store.updated_at,
        sentiments=sentiments,
        items=items,
        breaking=breaking,
    )
