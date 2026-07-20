"""Phase 4 news tests — store aggregation, scoring, and the pipeline with a
mocked analyzer (no network, no Claude call).

    python tests/test_news.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

from app.news.models import AnalyzedNews, NewsAnalysis, NewsItem
from app.news.store import NewsStore
from app.signals import scoring

NOW = int(time.time())


def _analyzed(sentiment, market, impact, mm=True, conf=90, published=None, analyzed=None) -> AnalyzedNews:
    pid = published if published is not None else NOW
    return AnalyzedNews(
        id=f"{sentiment}-{market}-{impact}-{pid}", title=f"{market} {sentiment} headline",
        link="http://x", source="Test", published=pid, analyzed_at=analyzed if analyzed is not None else NOW,
        sentiment=sentiment, affected_market=market, impact_score=impact,
        impact_duration="intraday", event_type="macro", confidence=conf,
        is_market_moving=mm, summary="…",
    )


def test_store_positive_aggregate():
    s = NewsStore(lookback_min=360)
    s.add_many([_analyzed("positive", "NIFTY", 70), _analyzed("positive", "BROAD", 60)])
    sent = s.sentiment("NIFTY")
    assert sent.items_considered == 2 and sent.net_score > 15 and sent.label == "positive"
    print(f"  POSITIVE -> net {sent.net_score} label {sent.label} items {sent.items_considered}")


def test_store_volatile_when_both_sides_strong():
    s = NewsStore(lookback_min=360)
    s.add_many([_analyzed("positive", "BANKNIFTY", 70), _analyzed("negative", "BANKNIFTY", 65)])
    sent = s.sentiment("BANKNIFTY")
    assert sent.label == "volatile", sent
    print(f"  VOLATILE -> net {sent.net_score} label {sent.label}")


def test_store_filters_low_impact_and_off_market():
    s = NewsStore(lookback_min=360)
    s.add_many([
        _analyzed("positive", "NIFTY", 20, mm=False),   # below impact bar
        _analyzed("negative", "OTHER", 80),             # wrong market
    ])
    sent = s.sentiment("NIFTY")
    assert sent.items_considered == 0 and sent.label == "neutral"
    print("  FILTER -> low-impact & off-market excluded")


def test_store_recency_decay_and_prune():
    s = NewsStore(lookback_min=120)  # 2h window
    s.add_many([_analyzed("positive", "NIFTY", 80, published=NOW - 3 * 3600)])  # 3h old → pruned
    sent = s.sentiment("NIFTY")
    assert sent.items_considered == 0, "stale item should be pruned/ignored"
    print("  PRUNE -> item older than lookback dropped")


def test_old_dated_article_stays_seen_after_analysis():
    # Review fix [2]: an old-*dated* article that was just analyzed must stay
    # 'seen' (pruned by analyzed_at, not published) so it isn't re-analyzed/re-billed
    # every poll — but its stale publish time keeps it out of the sentiment aggregate.
    s = NewsStore(lookback_min=120)  # 2h window
    old = _analyzed("negative", "NIFTY", 70, published=NOW - 5 * 3600, analyzed=NOW)
    s.add_many([old])
    assert s.seen(old.id), "freshly-analyzed old-dated article must remain seen"
    assert s.sentiment("NIFTY").items_considered == 0, "stale-by-publish item must not score"
    print("  RE-ANALYZE -> old-dated article stays seen; doesn't pollute sentiment")


def test_recent_filters_by_analyzed_at():
    # Review fix [6]: recent() must drop items whose analysis has aged out.
    s = NewsStore(lookback_min=120)
    stale = _analyzed("positive", "NIFTY", 60, analyzed=NOW - 5 * 3600)  # analyzed 5h ago
    fresh = _analyzed("positive", "NIFTY", 61, analyzed=NOW)             # distinct id
    s._items[stale.id] = stale
    s._items[fresh.id] = fresh
    ids = [n.id for n in s.recent()]
    assert fresh.id in ids and stale.id not in ids, ids
    print("  RECENT -> stale-analyzed item excluded from the news list")


def test_analyzer_dedup_and_chunking():
    # Review fixes [1]+[3]: duplicate indices are collapsed, and >_MAX_PER_CALL
    # items are split into chunks with indices remapped to global positions.
    from app.news.analyzer import NewsAnalyzer, _MAX_PER_CALL
    from app.news.models import NewsAnalysis as NA, NewsBatch, NewsItem

    def na(idx):
        return NA(index=idx, sentiment="neutral", affected_market="NIFTY", impact_score=10,
                  impact_duration="none", event_type="other", confidence=50,
                  is_market_moving=False, summary="x")

    class _Resp:
        def __init__(self, batch): self.parsed_output, self.stop_reason = batch, "end_turn"

    class _Msgs:
        def __init__(self, fn): self._fn = fn
        def parse(self, **kw): return self._fn(kw)

    class _Client:
        def __init__(self, fn): self.messages = _Msgs(fn)

    def item(i): return NewsItem(id=f"i{i}", title=f"h{i}", link="", source="S", published=NOW)

    # Dedup: model returns indices [0, 1, 1] for a 3-item chunk → keep 0 and 1.
    az = NewsAnalyzer("x")
    az._client = _Client(lambda kw: _Resp(NewsBatch(analyses=[na(0), na(1), na(1)])))
    assert sorted(a.index for a in az.analyze([item(i) for i in range(3)])) == [0, 1]

    # Chunking: 20 items → 2 calls; each fake returns 0.._MAX_PER_CALL-1 (range-clipped
    # to the chunk), remapped to global → contiguous 0..19.
    az2 = NewsAnalyzer("x")
    az2._client = _Client(lambda kw: _Resp(NewsBatch(analyses=[na(i) for i in range(_MAX_PER_CALL)])))
    got = sorted(a.index for a in az2.analyze([item(i) for i in range(20)]))
    assert got == list(range(20)), got
    print(f"  ANALYZER -> dedup + chunk/remap OK (20 items over {2} calls)")


def test_scoring_news_component_directional():
    s = NewsStore(lookback_min=360)
    s.add_many([_analyzed("positive", "NIFTY", 80)])
    sent = s.sentiment("NIFTY")
    ce = scoring._news(sent, bullish=True)    # positive news + CE → above neutral
    pe = scoring._news(sent, bullish=False)   # positive news + PE → below neutral
    assert ce.points > 5 and pe.points < 5, (ce.points, pe.points)
    none = scoring._news(None, bullish=True)
    assert none.points == 5.0
    print(f"  SCORE -> CE {ce.points}/10, PE {pe.points}/10, no-news {none.points}/10")


def test_pipeline_with_mocked_analyzer():
    from app.config import get_settings

    class FakeAnalyzer:
        def analyze(self, items):
            # Classify each fetched headline deterministically.
            return [NewsAnalysis(
                index=i, sentiment="negative", affected_market="BANKNIFTY", impact_score=75,
                impact_duration="intraday", event_type="regulatory", confidence=85,
                is_market_moving=True, summary="pressure on banks",
            ) for i, _ in enumerate(items)]

    from app.news.service import NewsService
    svc = NewsService(get_settings(), NewsStore(lookback_min=360))
    svc.analyzer = FakeAnalyzer()
    # bypass RSS network by injecting sources.fetch_all
    import app.news.service as service_mod
    fake_items = [NewsItem(id="a1", title="RBI tightens norms", link="http://x",
                           source="Test", published=NOW)]
    orig = service_mod.sources.fetch_all
    service_mod.sources.fetch_all = lambda feeds: fake_items
    try:
        added = svc.run_once()
    finally:
        service_mod.sources.fetch_all = orig
    assert added == 1
    sent = svc.store.sentiment("BANKNIFTY")
    assert sent.label == "negative" and sent.net_score < 0
    # re-run: already seen → nothing added
    service_mod.sources.fetch_all = lambda feeds: fake_items
    try:
        assert svc.run_once() == 0
    finally:
        service_mod.sources.fetch_all = orig
    print(f"  PIPELINE -> analyzed 1 headline → BANKNIFTY {sent.label} (net {sent.net_score}); dedupe works")


def _main():
    tests = [
        test_store_positive_aggregate, test_store_volatile_when_both_sides_strong,
        test_store_filters_low_impact_and_off_market, test_store_recency_decay_and_prune,
        test_old_dated_article_stays_seen_after_analysis, test_recent_filters_by_analyzed_at,
        test_analyzer_dedup_and_chunking,
        test_scoring_news_component_directional, test_pipeline_with_mocked_analyzer,
    ]
    failed = 0
    for t in tests:
        try:
            print(f"• {t.__name__}"); t()
        except AssertionError as e:
            failed += 1; print(f"  FAIL: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  ERROR: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
