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
    # --- Exhaustion & context gates (24-Jul: every blackout-day card fired at
    # the tail of a spent move; freshness of the QUOTE says nothing about
    # freshness of the MOVE) ---
    # Refuse a card whose invalidation level sits closer to spot than this many
    # ATRs: 0.4 points of room against a 19-point ATR died in 3 minutes. 0 = off.
    signal_min_invalidation_atr: float = Field(default=1.0, ge=0, alias="SIGNAL_MIN_INVALIDATION_ATR")
    # Refuse a card when the future is stretched more than this many ATRs from
    # its EMA20 — momentum confirmation at that distance is a chase. 0 = off.
    signal_max_extension_atr: float = Field(default=3.5, ge=0, alias="SIGNAL_MAX_EXTENSION_ATR")
    # Quiet period after a tick-feed gap ends: the first evaluations after a
    # blackout score a tape the engine never watched form. 0 = off.
    signal_post_gap_quiet_s: int = Field(default=600, ge=0, alias="SIGNAL_POST_GAP_QUIET_S")
    # --- Participation floors (25-Jul). Volume and OI are the only components
    # that measure whether anyone is IN the move; the other four describe the
    # chart, and a chart can look perfect on air. The weighted total lets 45
    # points of chart-shape outvote a dead tape — on 24-Jul every big paper
    # loser scored volume 2-6/15 under a full price-action 25/25, and 23-Jul's
    # weak card did the same with OI. A card below either floor is withheld
    # (reason shown) and paper-filled as a tagged "hollow" counterfactual, so
    # the ledger, not opinion, decides whether the floor stays. 0 = off. ---
    signal_min_volume_score: float = Field(default=7.0, ge=0, le=15, alias="SIGNAL_MIN_VOLUME_SCORE")
    signal_min_oi_score: float = Field(default=8.0, ge=0, le=20, alias="SIGNAL_MIN_OI_SCORE")
    # Scalp mode is PAPER-ONLY until flipped: journaling a scalp card live or
    # handing one to Kite is refused while false. The gate to flip it is
    # evidence, not enthusiasm — 50+ honest-fill paper scalps whose expectancy
    # survives the charges model (friction eats 20-30%% of gross at this cadence).
    scalp_live_enabled: bool = Field(default=False, alias="SCALP_LIVE_ENABLED")
    # Refuse a scalp card when estimated round-trip charges exceed this share
    # of the gross move to Target 1 (at the suggested size). 0 disables.
    scalp_max_friction_pct: float = Field(default=0.20, ge=0, le=1, alias="SCALP_MAX_FRICTION_PCT")
    # Derive intraday Target 1 from the paper book's recorded MFE distribution
    # (P75, clamped 8-27%) once >=30 clean samples exist; below that the static
    # RR_TARGET1 ladder stands. The advertised +27% T1 has never been hit
    # intraday; the +12% quick target is what lands. false = static only.
    signal_t1_from_excursions: bool = Field(default=True, alias="SIGNAL_T1_FROM_EXCURSIONS")
    # A card may only be issued on a premium quote at most this old. Added after
    # 21/22-Jul, when 5 of 9 cards priced their entry zone off a stale premium —
    # one literally at the previous day's close while the market opened 10%
    # higher — making the published zones unfillable and every level below them
    # wrong. Candle-verified: refs lagged the tape by 3 min to a full day.
    # 0 disables (tests / offline replay).
    signal_max_premium_age_s: int = Field(default=120, ge=0, alias="SIGNAL_MAX_PREMIUM_AGE_S")

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
    # Counts UNREALISED loss on open positions as well as booked loss.
    signal_daily_loss_limit: float = Field(default=0.0, ge=0, alias="SIGNAL_DAILY_LOSS_LIMIT")
    # Concurrent open positions after which no new signal is issued. Defaults ON
    # (unlike the rupee limits, which need capital context to set) because
    # stacking correlated bets needs no configuration to hurt you.
    signal_max_open_positions: int = Field(default=2, ge=0, alias="SIGNAL_MAX_OPEN_POSITIONS")
    # Unrealised drawdown across open positions that halts signals. 0 = disabled.
    signal_max_open_drawdown: float = Field(default=0.0, ge=0, alias="SIGNAL_MAX_OPEN_DRAWDOWN")
    # How far BELOW the trigger a protective stop's limit price sits. SL-M is
    # blocked for index options, so a stop is an SL (limit) order; too tight a
    # limit rests unfilled while the premium keeps falling, too wide invites a
    # terrible fill on a freak print. 5% of the trigger is the compromise.
    kite_sl_limit_buffer_pct: float = Field(
        default=0.05, gt=0, le=0.5, alias="KITE_SL_LIMIT_BUFFER_PCT"
    )

    # --- Off-desk signal delivery (see app/notify.py) ---
    # The browser alert only fires with the dashboard open. A webhook reaches
    # your phone. Empty (default) disables it entirely — no network call is made.
    # ntfy example:     https://ntfy.sh/<your-private-topic>
    # Telegram example: https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>
    alert_webhook_url: str = Field(default="", alias="ALERT_WEBHOOK_URL")
    # SIGNALS-ONLY second webhook (its own ntfy topic), for sharing cards with
    # another person WITHOUT handing over the master topic. Receives signal
    # cards and card retirements only — never watchdog pages, invalidation
    # nags, or armed confirmations (those describe YOUR positions and YOUR
    # infrastructure), and shared cards are sent WITHOUT the suggested-lots
    # line (sizing is TRADING_CAPITAL worked backwards through the stop — a
    # guest could reconstruct your account size from it). Revoke by rotating
    # just this topic; the primary stays untouched. Same format for both.
    alert_webhook_url_2: str = Field(default="", alias="ALERT_WEBHOOK_URL_2")
    # "text" posts a plain body (ntfy); "json" posts {title,text,message,...}.
    alert_webhook_format: str = Field(default="text", alias="ALERT_WEBHOOK_FORMAT")
    # Only push cards at or above this score. 0 = every issued card. The default
    # is deliberately below score_valid: the throttle already caps the engine at
    # 4 cards/day, so filtering further would only re-create the missed-signal
    # problem this exists to solve.
    alert_min_score: float = Field(default=0.0, ge=0, le=100, alias="ALERT_MIN_SCORE")
    # DEAD-FEED WATCHDOG. Page the phone when no tick has arrived for this many
    # seconds during market hours. Exists because of 23-Jul: a 77-minute silent
    # blackout contained the day's entire move, and nothing anywhere said the
    # engine had gone blind. caffeinate prevents sleep; this catches everything
    # else (network drops, socket death, token issues). 0 disables.
    feed_watchdog_age_s: int = Field(default=150, ge=0, alias="FEED_WATCHDOG_AGE_S")
    # THESIS-STALL TIME STOP (intraday only). If a trade has run this many
    # minutes without reaching its quick target, theta is winning: the monitor
    # recommends exit ("a thesis that is merely late still loses money" — the
    # missing fourth exit type). Recorded excursions back it: T1 almost never
    # arrives intraday and stalled trades bleed out slowly. 0 disables. The
    # paper engine auto-closes on it to gather evidence; the live journal only
    # gets the advisory unless "stall" is added to AUTO_CLOSE_TRIGGERS.
    stall_exit_minutes: int = Field(default=45, ge=0, alias="STALL_EXIT_MINUTES")

    # --- Journal auto-close (advisory bookkeeping; places NO orders) ---
    # When a plan trigger fires, close the journal row so P&L, the daily loss
    # limit and the open-position circuit breakers reflect it without waiting
    # for a manual click. Tradewell cannot observe your real fill, so the row
    # is flagged `auto_closed` and can be reopened.
    auto_close_journal: bool = Field(default=True, alias="AUTO_CLOSE_JOURNAL")
    # Comma-separated subset of: stop, target1, target2, invalidation,
    # time_exit, stall. "stall" is deliberately NOT in the default — the paper
    # book auto-closes on it to gather evidence first; opt the live journal in
    # only once that evidence supports it.
    auto_close_triggers: str = Field(
        default="stop,target1,invalidation", alias="AUTO_CLOSE_TRIGGERS"
    )

    # --- Broker reconciliation (READ-ONLY; needs no order permission) ---
    # Mirrors Zerodha's position book into the journal so open/closed state is
    # observed rather than inferred from price. Once a row is confirmed at the
    # broker, the broker becomes authoritative for it and price-based
    # auto-close is suppressed — otherwise the two fight each other.
    broker_reconcile: bool = Field(default=True, alias="BROKER_RECONCILE")
    broker_reconcile_seconds: float = Field(
        default=15.0, gt=0, alias="BROKER_RECONCILE_SECONDS"
    )

    # --- Paper trading (SIMULATED; no broker, no money, no orders) ---
    # Takes every issued signal in simulation and exits it by the plan, so the
    # engine can be forward-tested on live ticks while you are away. Paper
    # positions live in their own store and never reach the real journal, the
    # realised P&L, or the circuit breakers.
    paper_trading: bool = Field(default=False, alias="PAPER_TRADING")
    # Lots per simulated trade. 0 = use the card's suggested_lots.
    paper_lots: int = Field(default=1, ge=0, alias="PAPER_LOTS")
    # Charged against the fill on BOTH legs. The tape's last price is not the
    # ask you would pay, and a paper book that ignores that is a fantasy.
    paper_slippage_pct: float = Field(default=0.004, ge=0, le=0.1, alias="PAPER_SLIPPAGE_PCT")

    # --- Position sizing guidance ---
    # Capital the sizing suggestion is computed against. 0 = unknown -> no
    # suggestion is shown (never guess a size on someone's behalf).
    trading_capital: float = Field(default=0.0, ge=0, alias="TRADING_CAPITAL")
    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=10, alias="RISK_PER_TRADE_PCT")
    # DEPLOYABLE premium budget for the day — a different question from
    # TRADING_CAPITAL. Capital answers "how much may I LOSE per trade" (risk
    # sizing); the fund answers "how many lots can I actually BUY at this
    # premium" (affordability), and prefills the card's lots so the Kite basket
    # opens with the right quantity. Runtime-editable from the dashboard
    # (risk-limits panel); this is only the .env baseline. 0 = feature off.
    trading_fund: float = Field(default=0.0, ge=0, alias="TRADING_FUND")
    # Risk model
    premium_sl_pct: float = Field(default=0.18, alias="PREMIUM_SL_PCT")
    # EARLY partial-book level, as a fraction above YOUR fill. Reaching it books
    # half the lots and moves the stop to entry, so the rest runs risk-free.
    # Exists because Target 1 (+27%) is too far to manage a trade that spikes and
    # gives it all back: on 21-Jul a position ran +31% and still closed at a
    # loss. Booking half at +12% converts that into a win without capping the
    # runner, which is what simply lowering Target 1 would do. 0 disables.
    # Tune it from GET /trades/excursion once a real sample exists.
    quick_target_pct: float = Field(default=0.12, ge=0, lt=1, alias="QUICK_TARGET_PCT")

    # Which stop actually ENDS a trade before Target 1.
    #   "underlying" — the index invalidation level is primary; the premium stop
    #                  is demoted to a disaster backstop at premium_disaster_pct.
    #   "premium"    — the old behaviour: premium_sl_pct ends the trade.
    # Measured 21-Jul-2026: a 0.14% index range produced an 83% premium swing on
    # a 0DTE contract, and the 18% premium stop — worth ~10 index points against
    # a 35-point noise band — was touched 7 times while the index invalidation
    # was never breached once. On expiry-day gamma the premium stop measures
    # noise; the index level measures the thesis.
    stop_primary: str = Field(default="underlying", alias="STOP_PRIMARY")
    # The backstop, as a fraction of YOUR fill. Only reached when the premium
    # collapses without the index invalidating — i.e. theta/IV, not direction.
    premium_disaster_pct: float = Field(default=0.45, gt=0, lt=1, alias="PREMIUM_DISASTER_PCT")
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
