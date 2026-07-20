"""In-memory store of analyzed news + per-symbol sentiment aggregation."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from app.news.models import AnalyzedNews, NewsSentiment

log = logging.getLogger("tradewell.news")

# Dedupe memory persists across restarts — without it every backend restart
# re-bills the entire feed backlog (~100 headlines) to Claude.
_SEEN_PATH = Path(__file__).resolve().parents[2] / ".news_seen.json"

# An item contributes to a symbol's sentiment if it targets that index or the
# broad market and clears this impact/relevance bar (filters routine noise).
_MIN_IMPACT = 40


def _now() -> int:
    return int(time.time())


# RSS feeds retain entries for 24-48h, so the "have we analyzed this?" memory
# must outlive the (much shorter) sentiment window — otherwise every headline
# gets re-sent to Claude (re-billed) each time it falls out of the 6h window
# while still sitting in the feed.
_SEEN_TTL = 48 * 3600


class NewsStore:
    def __init__(self, lookback_min: int = 360, seen_path: Path | None = None) -> None:
        """seen_path=None -> memory-only dedupe (unit tests / ad-hoc stores).
        The module singleton below passes the real path for persistence."""
        self._lock = threading.Lock()
        self._items: dict[str, AnalyzedNews] = {}
        self._seen: dict[str, int] = {}     # item_id -> first-seen epoch (48h TTL)
        self._seen_path = seen_path
        self._lookback = lookback_min * 60
        self._updated_at: int | None = None
        self._load_seen()

    def _load_seen(self) -> None:
        if self._seen_path is None:
            return
        try:
            if self._seen_path.exists():
                raw = json.loads(self._seen_path.read_text())
                cutoff = _now() - _SEEN_TTL
                self._seen = {k: int(v) for k, v in raw.items() if int(v) >= cutoff}
        except Exception as exc:  # pragma: no cover — corrupt cache: start empty
            log.warning("news seen-cache unreadable, starting fresh: %s", exc)

    def _save_seen_locked(self) -> None:
        if self._seen_path is None:
            return
        try:
            tmp = self._seen_path.with_name(self._seen_path.name + ".tmp")
            tmp.write_text(json.dumps(self._seen))
            tmp.replace(self._seen_path)
        except Exception as exc:  # pragma: no cover
            log.warning("news seen-cache write failed: %s", exc)

    def seen(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self._seen

    def mark_seen(self, item_ids: list[str]) -> None:
        """Record ids as analyzed (or skipped) so they're never re-billed."""
        if not item_ids:
            return
        now = _now()
        with self._lock:
            for i in item_ids:
                self._seen.setdefault(i, now)
            self._save_seen_locked()

    def add_many(self, analyzed: list[AnalyzedNews]) -> None:
        if not analyzed:
            return
        now = _now()
        with self._lock:
            for a in analyzed:
                self._items[a.id] = a
                self._seen.setdefault(a.id, now)
            self._updated_at = now
            self._prune_locked()
            self._save_seen_locked()

    def _prune_locked(self) -> None:
        now = _now()
        # Display/sentiment items age out on the lookback window…
        cutoff = now - self._lookback
        for k in [k for k, v in self._items.items() if v.analyzed_at < cutoff]:
            self._items.pop(k, None)
        # …but the dedupe memory lives much longer than the feed retains entries.
        seen_cutoff = now - _SEEN_TTL
        for k in [k for k, ts in self._seen.items() if ts < seen_cutoff]:
            self._seen.pop(k, None)

    def recent(self, limit: int = 40) -> list[AnalyzedNews]:
        now = _now()
        with self._lock:
            items = [a for a in self._items.values() if (now - a.analyzed_at) <= self._lookback]
        items.sort(key=lambda a: a.published, reverse=True)
        return items[:limit]

    def sentiment(self, symbol: str) -> NewsSentiment:
        symbol = symbol.upper()
        now = _now()
        with self._lock:
            items = list(self._items.values())

        relevant = [
            a for a in items
            if a.affected_market in (symbol, "BROAD")
            and (a.is_market_moving or a.impact_score >= _MIN_IMPACT)
            and (now - a.published) <= self._lookback
        ]
        if not relevant:
            return NewsSentiment(symbol=symbol, net_score=0.0, label="neutral",
                                 items_considered=0, updated_at=self._updated_at)

        num = den = 0.0
        max_pos = max_neg = 0.0
        for a in relevant:
            sign = 1.0 if a.sentiment == "positive" else -1.0 if a.sentiment == "negative" else 0.0
            # Clamp both ends so a future-dated feed timestamp can't over-weight.
            recency = min(1.0, max(0.0, 1.0 - (now - a.published) / self._lookback))
            weight = (a.confidence / 100.0) * recency
            num += sign * a.impact_score * weight
            den += weight
            if sign > 0:
                max_pos = max(max_pos, a.impact_score)
            elif sign < 0:
                max_neg = max(max_neg, a.impact_score)

        net = round(max(-100.0, min(100.0, num / den)) if den else 0.0, 1)
        if max_pos >= 50 and max_neg >= 50:
            label = "volatile"
        elif net >= 15:
            label = "positive"
        elif net <= -15:
            label = "negative"
        else:
            label = "neutral"

        top = [a.title for a in sorted(relevant, key=lambda a: a.impact_score, reverse=True)[:3]]
        return NewsSentiment(symbol=symbol, net_score=net, label=label,
                             items_considered=len(relevant), top_headlines=top,
                             updated_at=self._updated_at)

    @property
    def updated_at(self) -> int | None:
        return self._updated_at


news_store = NewsStore(seen_path=_SEEN_PATH)
