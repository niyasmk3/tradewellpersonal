"""RSS news ingestion.

Pulls headlines from the configured Indian-market RSS feeds and normalises them
to `NewsItem`s with a stable dedupe id. Network I/O is blocking, so the service
calls this via ``asyncio.to_thread``.
"""
from __future__ import annotations

import calendar
import hashlib
import logging
import time
import urllib.request

from app.news.models import NewsItem

log = logging.getLogger("tradewell.news")

_FETCH_TIMEOUT = 10.0
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _item_id(title: str, link: str) -> str:
    return hashlib.sha1(f"{title}|{link}".encode("utf-8", "ignore")).hexdigest()[:16]


def _published_epoch(entry) -> int:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return int(calendar.timegm(st))  # feedparser struct_time is UTC
            except Exception:
                pass
    return int(time.time())


def fetch_feed(url: str) -> list[NewsItem]:
    import feedparser  # imported lazily so the app runs without the dep installed

    items: list[NewsItem] = []
    try:
        # Fetch the bytes ourselves with a hard timeout — feedparser's own URL
        # fetching has none, and one stalled host would hang the news loop
        # forever (the poll task never returns, no error ever logged).
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            raw = resp.read()
        parsed = feedparser.parse(raw)
        source = (parsed.feed.get("title") if parsed.feed else None) or url
        for entry in parsed.entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title:
                continue
            items.append(
                NewsItem(
                    id=_item_id(title, link),
                    title=title,
                    link=link,
                    source=source,
                    published=_published_epoch(entry),
                )
            )
    except Exception as exc:  # a bad feed shouldn't kill the poll
        log.warning("feed fetch failed for %s: %s", url, exc)
    return items


def fetch_all(feeds: list[str]) -> list[NewsItem]:
    seen: set[str] = set()
    out: list[NewsItem] = []
    for url in feeds:
        for item in fetch_feed(url):
            if item.id not in seen:
                seen.add(item.id)
                out.append(item)
    return out
