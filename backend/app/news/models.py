"""Phase 4 news-intelligence models.

Claude classifies each headline into a `NewsAnalysis` (the structured-output
schema). The trading engine — not Claude — still decides strike, stop-loss, and
targets; news only feeds the 0-10 news component of the signal score.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Sentiment = Literal["positive", "negative", "neutral"]
AffectedMarket = Literal["NIFTY", "BANKNIFTY", "FINNIFTY", "BROAD", "OTHER"]
ImpactDuration = Literal["intraday", "multiday", "lasting", "none"]


class NewsItem(BaseModel):
    """A raw headline pulled from an RSS feed."""
    id: str                      # stable hash of title+link (dedupe key)
    title: str
    link: str
    source: str
    published: int               # epoch seconds


class NewsAnalysis(BaseModel):
    """Claude's structured classification of one headline (the parse() schema)."""
    index: int                   # position in the batch (maps back to the NewsItem)
    sentiment: Sentiment
    affected_market: AffectedMarket
    impact_score: int            # 0-100: expected magnitude of intraday price impact
    impact_duration: ImpactDuration
    event_type: str              # regulatory / earnings / macro / global / policy / other
    confidence: int              # 0-100
    is_market_moving: bool       # strong enough to influence a signal?
    conflicts_with_technicals: bool = False
    summary: str


class NewsBatch(BaseModel):
    """Wrapper so Claude returns one analysis per input headline."""
    analyses: list[NewsAnalysis]


class AnalyzedNews(BaseModel):
    """A headline merged with its analysis — stored and shown in the UI."""
    id: str
    title: str
    link: str
    source: str
    published: int
    analyzed_at: int
    sentiment: Sentiment
    affected_market: AffectedMarket
    impact_score: int
    impact_duration: ImpactDuration
    event_type: str
    confidence: int
    is_market_moving: bool
    summary: str


class NewsSentiment(BaseModel):
    """Per-symbol aggregate over the recent lookback window (feeds scoring)."""
    symbol: str
    net_score: float             # -100 (bearish) .. +100 (bullish)
    label: str                   # positive / negative / neutral / volatile
    items_considered: int
    top_headlines: list[str] = []
    updated_at: Optional[int] = None


class NewsResponse(BaseModel):
    enabled: bool
    updated_at: Optional[int] = None
    sentiments: list[NewsSentiment] = []
    items: list[AnalyzedNews] = []
    # High-impact market-moving headlines (impact >= NEWS_BREAKING_IMPACT). The
    # dashboard alerts on these the moment they appear — the reason this exists
    # is a US-Iran story that moved NIFTY while the pipeline stayed silent.
    breaking: list[AnalyzedNews] = []
    note: Optional[str] = None
