"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Kite Connect
    kite_api_key: str = Field(default="", alias="KITE_API_KEY")
    kite_api_secret: str = Field(default="", alias="KITE_API_SECRET")
    kite_access_token: str = Field(default="", alias="KITE_ACCESS_TOKEN")

    # Tracking
    track_underlyings: str = Field(default="NIFTY,BANKNIFTY,FINNIFTY", alias="TRACK_UNDERLYINGS")
    option_chain_depth: int = Field(default=10, ge=1, alias="OPTION_CHAIN_DEPTH")
    # gt=0: a zero/negative interval turns a poll loop into a busy loop that
    # hammers Kite (and, for news, bills Claude) with no delay.
    option_poll_seconds: float = Field(default=5.0, gt=0, alias="OPTION_POLL_SECONDS")
    broadcast_seconds: float = Field(default=1.0, gt=0, alias="BROADCAST_SECONDS")

    # Networking
    frontend_origin: str = Field(default="http://localhost:3000", alias="FRONTEND_ORIGIN")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # --- Phase 2: signal engine ---
    # Underlyings the signal engine runs on (Phase 2 = NIFTY only).
    signal_underlyings: str = Field(default="NIFTY", alias="SIGNAL_UNDERLYINGS")
    # Trading modes to evaluate (toggle-able in the UI).
    signal_modes: str = Field(default="intraday,positional", alias="SIGNAL_MODES")
    signal_timeframe: str = Field(default="3m", alias="SIGNAL_TIMEFRAME")
    signal_eval_seconds: float = Field(default=5.0, gt=0, alias="SIGNAL_EVAL_SECONDS")
    signal_validity_seconds: int = Field(default=480, alias="SIGNAL_VALIDITY_SECONDS")
    # Score thresholds (0-100): >=valid -> tradeable, >=wait -> wait for confirmation.
    # Raised 70 -> 78 on 2026-07-20: at 70 the engine issued 11 cards in under 3
    # hours (design goal is 1-4/day) and the highest-confidence ones still lost.
    score_valid: int = Field(default=78, alias="SCORE_VALID")
    score_wait: int = Field(default=70, alias="SCORE_WAIT")

    # --- Signal throttle (added 2026-07-20 after an 11-signal / -Rs70k day) ---
    # A tradeable score is necessary but NOT sufficient: these gates cap how
    # often the engine may speak, so a choppy tape can't produce a stream of
    # mutually-contradicting cards.
    signal_max_per_day: int = Field(default=4, ge=1, alias="SIGNAL_MAX_PER_DAY")
    # Minimum gap between two issued signals for the same (symbol, mode).
    signal_min_gap_s: int = Field(default=900, ge=0, alias="SIGNAL_MIN_GAP_S")
    # Quiet period after a card expires/cancels before a new one may be adopted.
    signal_cooldown_s: int = Field(default=600, ge=0, alias="SIGNAL_COOLDOWN_S")
    # Refuse an OPPOSITE-direction signal within this window of the last one —
    # six PE cards followed by five CE cards is the engine whipsawing, not an edge.
    signal_flip_guard_s: int = Field(default=1800, ge=0, alias="SIGNAL_FLIP_GUARD_S")

    # --- Circuit breakers (stop issuing entirely) ---
    signal_max_consecutive_losses: int = Field(default=2, ge=1, alias="SIGNAL_MAX_CONSECUTIVE_LOSSES")
    # Rupee loss (positive number) that halts signals for the day. 0 = disabled.
    signal_daily_loss_limit: float = Field(default=0.0, ge=0, alias="SIGNAL_DAILY_LOSS_LIMIT")

    # --- Position sizing guidance ---
    # Capital the sizing suggestion is computed against. 0 = unknown -> no
    # suggestion is shown (never guess a size on someone's behalf).
    trading_capital: float = Field(default=0.0, ge=0, alias="TRADING_CAPITAL")
    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=10, alias="RISK_PER_TRADE_PCT")
    # Risk model
    premium_sl_pct: float = Field(default=0.18, alias="PREMIUM_SL_PCT")
    rr_target1: float = Field(default=1.5, alias="RR_TARGET1")
    rr_target2: float = Field(default=2.5, alias="RR_TARGET2")
    # Strike liquidity guards
    strike_min_oi: float = Field(default=100_000, alias="STRIKE_MIN_OI")
    strike_max_spread_pct: float = Field(default=0.015, alias="STRIKE_MAX_SPREAD_PCT")

    # --- Phase 4: news intelligence ---
    # Enable news analysis. Also requires ANTHROPIC_API_KEY for the Claude calls;
    # without it the news score stays neutral and the pipeline is a no-op.
    news_enabled: bool = Field(default=True, alias="NEWS_ENABLED")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    # Claude model for sentiment classification. Opus is the default; set to
    # claude-haiku-4-5 for cheaper high-volume classification.
    news_model: str = Field(default="claude-opus-4-8", alias="NEWS_MODEL")
    # Indian-market RSS feeds (comma-separated). Verified live & fresh 2026-07-19
    # across 3 publishers (ET, Livemint, BusinessLine). NOTE: Moneycontrol RSS is
    # ~2yr stale and Business Standard RSS is dead/blocked — both were removed.
    # Per-stock platforms (Trendlyne, Tickertape) expose no RSS/API, so they can't
    # feed this pipeline; Tickertape's Market Mood Index is a separate gauge, not news.
    news_feeds: str = Field(
        default=(
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms,"          # ET Markets
            "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms,"     # ET Economy (macro)
            "https://www.livemint.com/rss/markets,"                                          # Livemint Markets
            "https://www.thehindubusinessline.com/markets/feeder/default.rss,"               # BusinessLine Markets
            # Geopolitics / global macro. Added 2026-07-20 after a US-Iran story
            # moved NIFTY and NOTHING in the store had it: every feed above is a
            # *markets* desk, and a war headline breaks on a world desk first.
            "https://www.thehindubusinessline.com/news/world/feeder/default.rss,"            # BusinessLine World
            "https://www.thehindubusinessline.com/economy/feeder/default.rss"                # BusinessLine Economy
        ),
        alias="NEWS_FEEDS",
    )
    # 60s, not 300s: measured publish→analyzed lag was 8-10 min at 300s, useless
    # for a breaking macro event. Cost is bounded by NEW headlines (the 48h
    # dedupe means a quiet poll bills nothing), not by poll frequency.
    news_poll_seconds: float = Field(default=60.0, gt=0, alias="NEWS_POLL_SECONDS")
    news_lookback_min: int = Field(default=360, ge=1, alias="NEWS_LOOKBACK_MIN")   # 6h window
    news_max_batch: int = Field(default=15, ge=1, alias="NEWS_MAX_BATCH")
    # A headline at/above this impact with is_market_moving is "breaking": it is
    # surfaced immediately and the dashboard alerts on it.
    news_breaking_impact: int = Field(default=70, ge=1, le=100, alias="NEWS_BREAKING_IMPACT")

    # Market Mood Index (Tickertape) — read-only context gauge, no API key needed.
    # Best-effort external fetch; set false to disable the outbound call entirely.
    mmi_enabled: bool = Field(default=True, alias="MMI_ENABLED")

    # --- Phase 5: backtesting ---
    # Point slippage applied (against us) on backtest entry and exit fills.
    backtest_slippage_pts: float = Field(default=1.5, ge=0.0, alias="BACKTEST_SLIPPAGE_PTS")
    # Max bars a positional backtest trade is held before a time-based exit.
    backtest_max_hold_bars: int = Field(default=80, ge=1, alias="BACKTEST_MAX_HOLD_BARS")

    @property
    def news_feed_list(self) -> list[str]:
        return [f.strip() for f in self.news_feeds.split(",") if f.strip()]

    @property
    def news_active(self) -> bool:
        return self.news_enabled and bool(self.anthropic_api_key)

    @property
    def signal_symbols(self) -> list[str]:
        return [u.strip().upper() for u in self.signal_underlyings.split(",") if u.strip()]

    @property
    def signal_mode_list(self) -> list[str]:
        return [m.strip().lower() for m in self.signal_modes.split(",") if m.strip()]

    @property
    def underlyings(self) -> list[str]:
        return [u.strip().upper() for u in self.track_underlyings.split(",") if u.strip()]

    @property
    def has_access_token(self) -> bool:
        return bool(self.kite_access_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
