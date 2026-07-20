"""News pipeline: fetch RSS -> analyze new headlines with Claude -> store."""
from __future__ import annotations

import logging
import time

from app.config import Settings
from app.news import sources
from app.news.analyzer import NewsAnalyzer
from app.news.models import AnalyzedNews
from app.news.store import NewsStore

log = logging.getLogger("tradewell.news")


class NewsService:
    def __init__(self, cfg: Settings, store: NewsStore) -> None:
        self.cfg = cfg
        self.store = store
        self.analyzer = NewsAnalyzer(cfg.news_model, cfg.anthropic_api_key)

    def run_once(self) -> int:
        """Fetch, analyze the freshest unseen headlines, store. Returns #added.
        Blocking (network + Claude) — call via asyncio.to_thread."""
        items = sources.fetch_all(self.cfg.news_feed_list)
        now0 = int(time.time())
        unseen = [it for it in items if not self.store.seen(it.id)]

        # Headlines already older than the sentiment window would be billed to
        # Claude and then filtered out of the aggregate — mark seen and skip.
        lookback_s = self.cfg.news_lookback_min * 60
        too_old = [it for it in unseen if (now0 - it.published) > lookback_s]
        if too_old:
            self.store.mark_seen([it.id for it in too_old])
        fresh = [it for it in unseen if (now0 - it.published) <= lookback_s]

        fresh.sort(key=lambda it: it.published, reverse=True)
        fresh = fresh[: self.cfg.news_max_batch]
        if not fresh:
            return 0

        analyses = self.analyzer.analyze(fresh)
        # Everything we SENT is spent — mark it all seen (not just what came
        # back) so a partial Claude response can't cause re-billing next poll.
        self.store.mark_seen([it.id for it in fresh])
        now = int(time.time())
        merged: list[AnalyzedNews] = []
        for a in analyses:
            it = fresh[a.index]
            merged.append(AnalyzedNews(
                id=it.id, title=it.title, link=it.link, source=it.source,
                published=it.published, analyzed_at=now,
                sentiment=a.sentiment, affected_market=a.affected_market,
                impact_score=a.impact_score, impact_duration=a.impact_duration,
                event_type=a.event_type, confidence=a.confidence,
                is_market_moving=a.is_market_moving, summary=a.summary,
            ))
        self.store.add_many(merged)
        if merged:
            log.info("news: analyzed %d new headline(s)", len(merged))
        return len(merged)
