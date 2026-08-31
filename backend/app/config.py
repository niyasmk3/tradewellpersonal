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
    # --- Week-1 forensics gates (28-Jul) ---
    # No NEW intraday/scalp cards at/after this IST time: every card issued
    # 14:27+ in the audited week lost (7-for-7, -Rs1,416) — they run into the
    # 15:20 time exit with no runway. "HH:MM"; empty = off. Positional exempt.
    signal_entry_cutoff_ist: str = Field(default="14:15", alias="SIGNAL_ENTRY_CUTOFF_IST")
    # Re-fire guard: after a clean paper/live fill on the same underlying +
    # direction (strike within 2 steps) STOPS OUT or invalidates, block a new
    # card on that thesis for this many seconds (same IST day). The Tue 28
    # tape re-fired 24000 PE twice after its first stop and lost all three
    # (-Rs828). 0 = off.
    signal_refire_guard_s: int = Field(default=7200, ge=0, alias="SIGNAL_REFIRE_GUARD_S")
    # Early de-risk for intraday/scalp: once MFE reaches this fraction above
    # entry (before the +12% quick target), move the stop to entry — the
    # audited week had TEN fills peak between +1.9% and +11.3% and close
    # negative, every one. Mirrors the positional +5% breakeven rule. 0 = off.
    early_derisk_mfe_pct: float = Field(default=0.05, ge=0, le=0.2, alias="EARLY_DERISK_MFE_PCT")
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
    # INTRADAY DEMOTED TO PAPER AUDITION (31-Aug, user decision on August
    # evidence: 14 clean fills, 14% WR, -Rs5,050, ZERO Target-1 exits in 31
    # days, avg loss 7x avg win). Same re-entry bar as scalp's audition: 50
    # honest paper fills at positive net expectancy flip this to true — the
    # paper book keeps filling and grading intraday cards throughout.
    intraday_live_enabled: bool = Field(default=False, alias="INTRADAY_LIVE_ENABLED")
    # EVENING-POSITIONAL SIZE CUT (31-Aug): positional entries at/after 14:30
    # carry overnight-gap risk no stop can act on (18-Aug: one 15:21 entry
    # gapped to -50.8%, -Rs3,884 — more than the rest of the month's
    # positional book combined). Suggested lots are scaled by this factor
    # until the overnight-hold ledger renders its verdict. 1.0 disables.
    evening_positional_size_factor: float = Field(
        default=0.5, ge=0.1, le=1.0, alias="EVENING_POSITIONAL_SIZE_FACTOR")
    # Refuse a scalp card when estimated round-trip charges exceed this share
    # of the gross move to Target 1 (at the suggested size). 0 disables.
    scalp_max_friction_pct: float = Field(default=0.20, ge=0, le=1, alias="SCALP_MAX_FRICTION_PCT")
    # BANK-THE-QUICK-TARGET policy (25-Jul, prototype): on a SINGLE-LOT trade,
    # exit the whole position at the +12%% quick target instead of going
    # risk-free and trailing. Exists because 1 lot cannot "book half": the
    # ratchet only locks value ABOVE the quick target, so a trade peaking near
    # +12%% still round-trips to ~breakeven. Default OFF — the paper summary's
    # exit_ab block measures the paired counterfactual on every recorded fill,
    # and the ledger (30+ diverged fills), not a hunch, decides the flip. Note:
    # the A/B is only measurable while this is off; banking ends the trade at
    # the quick target, so the ratchet's path is never observed after a flip.
    quick_bank_single_lot: bool = Field(default=False, alias="QUICK_BANK_SINGLE_LOT")
    # Derive intraday Target 1 from the paper book's recorded MFE distribution
    # (P75, clamped 8-27%) once >=30 clean samples exist; below that the static
    # RR_TARGET1 ladder stands. The advertised +27% T1 has never been hit
    # intraday; the +12% quick target is what lands. false = static only.
    signal_t1_from_excursions: bool = Field(default=True, alias="SIGNAL_T1_FROM_EXCURSIONS")
    # Level-touch callouts: BUY/CEILING pushes when live spot tests one of the
    # Patterns Module's strongest S/R levels (>=5 distinct touch-days, >=60%
    # hold rate). Context alerts with their own odds attached — never cards.
    level_alerts_enabled: bool = Field(default=True, alias="LEVEL_ALERTS_ENABLED")
    # Per-level re-fire spacing; the edge trigger + re-arm band do the fine
    # de-bouncing, this stops a level camped on all afternoon from repeating.
    level_alert_cooldown_s: int = Field(default=1800, ge=0, alias="LEVEL_ALERT_COOLDOWN_S")
    # WATCH->CONFIRM persistence (audit P2-3, sized 05-Aug): a card may only
    # issue once its direction's score has held the gate for this many
    # CONSECUTIVE closed bars. The 60d replay: 58% of gate-crossings last
    # exactly one bar and lose -0.41R at 30% WR (the 05-Aug 11:03 card's
    # class); persistent episodes are the engine's only positive class.
    # First-bar cards are shadow-booked to their own "confirm" ledger.
    # 0 or 1 disables. Intraday/scalp only — positional's 15m bars are slow.
    signal_confirm_bars: int = Field(default=2, ge=0, alias="SIGNAL_CONFIRM_BARS")
    # Blind-spot instrumentation (audit P1-2): one JSONL line per closed bar
    # per mode with both directions' scores, the regime vote and any veto —
    # the dataset that classifies missed moves (36/41 had NO candidate; 27 of
    # those are unattributable without this). Retention in days; 0 disables
    # recording and prunes the file at the next boot.
    eval_trace_days: int = Field(default=30, ge=0, alias="EVAL_TRACE_DAYS")
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

    # --- Former circuit breakers (removed 25-Jul at the user's request) ---
    # The live loss/streak/open-position/drawdown breakers that halted signals
    # after a bad run are gone. Two of the four values survive with NARROWER,
    # non-breaker roles; the other two (consecutive-losses, open-drawdown) are
    # deleted outright. A stale SIGNAL_MAX_CONSECUTIVE_LOSSES/…_DRAWDOWN left in
    # .env is simply ignored (extra="ignore").
    #   * No longer halts signals — only supplies the "% of your daily limit"
    #     context in the oversize-journal warning and on the card. 0 = no context.
    signal_daily_loss_limit: float = Field(default=0.0, ge=0, alias="SIGNAL_DAILY_LOSS_LIMIT")
    #   * No longer gates live signals — now caps ONLY the paper simulator's
    #     concurrent positions per mode (the evidence engine). 0 = uncapped.
    signal_max_open_positions: int = Field(default=2, ge=0, alias="SIGNAL_MAX_OPEN_POSITIONS")
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
    # Send a verification ping to the webhook(s) at every feed start. True is
    # the safety default (the ping is what proves delivery after 23-Jul's
    # silently-dead-all-day webhook). false = signal pushes only: no test
    # pings; the armed chip then stays grey until the FIRST REAL signal push
    # answers 2xx — delivery is still proven, just on the first card instead
    # of at startup.
    alert_startup_ping: bool = Field(default=True, alias="ALERT_STARTUP_PING")
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
    # STOP-BASIS PAIRED A/B (audit P1-5). STOP_PRIMARY has been flipped twice
    # on single-trade evidence; this books a shadow twin of every clean paper
    # fill running the OPPOSITE stop basis, so the choice gets the same
    # 30-diverged-fill discipline as the exit-policy A/B. Costs nothing live.
    stop_ab_paired: bool = Field(default=True, alias="STOP_AB_PAIRED")
    # STOP-CALIBRATION SHADOW (05-Aug). T1 already self-tunes from the paper
    # book's MFE distribution; this is the stop's mirror — at 30+ clean
    # intraday fills a calibrated SL%% is fitted from where WINNERS bottomed
    # (MAE P90 + buffer), and every clean intraday fill books a twin identical
    # except that one stop level. The stop_calib ledger pairs them; the live
    # ladder never moves until that ledger wins at 30+ diverged pairs.
    stop_calib_shadow: bool = Field(default=True, alias="STOP_CALIB_SHADOW")
    # --- Post-close ops (05-Aug) ---
    # Nightly state tarball (paper book, journals, archives, .env, patterns
    # candles) into this folder — point it at an iCloud-synced path and any
    # laptop becomes disposable. Empty = backup off.
    state_backup_dir: str = Field(default="", alias="STATE_BACKUP_DIR")
    state_backup_keep: int = Field(default=14, ge=1, alias="STATE_BACKUP_KEEP")
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
    # The dashboard's ask-anything box (app/assistant.py). Haiku by default:
    # explanation questions at chat cadence — cheap, fast, good enough; the
    # news module keeps its own, bigger model for classification.
    chat_model: str = Field(default="claude-haiku-4-5-20251001", alias="CHAT_MODEL")
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

    # --- Iron Condor module (docs/iron-condor-spec-2026-08-11.md) ---
    # Advisory-only, shadow-first: OFF by default; cards render on /condor
    # labelled TRIAL and are never pushed. Every threshold below is a
    # pre-registered spec default — changes follow the house 30-sample rule.
    condor_enabled: bool = Field(default=False, alias="CONDOR_ENABLED")
    condor_symbols: str = Field(default="NIFTY", alias="CONDOR_SYMBOLS")
    condor_profile: str = Field(default="balanced", alias="CONDOR_PROFILE")
    condor_eval_s: float = Field(default=60.0, gt=0, alias="CONDOR_EVAL_S")
    condor_min_dte: int = Field(default=2, ge=0, alias="CONDOR_MIN_DTE")
    condor_max_dte: int = Field(default=7, ge=1, alias="CONDOR_MAX_DTE")
    condor_score_min: float = Field(default=80, ge=0, le=100, alias="CONDOR_SCORE_MIN")
    condor_watch_min: float = Field(default=70, ge=0, le=100, alias="CONDOR_WATCH_MIN")
    condor_max_open: int = Field(default=1, ge=0, alias="CONDOR_MAX_OPEN")
    condor_lots: int = Field(default=1, ge=1, alias="CONDOR_LOTS")
    # Credit floor as a fraction of wing width, and the EM distance floor for
    # short strikes (MILD regimes force 1.0x on the drift side regardless).
    condor_min_credit_pct: float = Field(default=0.20, gt=0, lt=1, alias="CONDOR_MIN_CREDIT_PCT")
    condor_min_em_dist: float = Field(default=0.75, gt=0, alias="CONDOR_MIN_EM_DIST")
    condor_wing_widths: str = Field(default="100,150,200,250,300", alias="CONDOR_WING_WIDTHS")
    condor_min_oi_short: float = Field(default=100_000, ge=0, alias="CONDOR_MIN_OI_SHORT")
    condor_min_oi_wing: float = Field(default=50_000, ge=0, alias="CONDOR_MIN_OI_WING")
    condor_max_spread_pct_short: float = Field(default=0.05, gt=0, alias="CONDOR_MAX_SPREAD_PCT_SHORT")
    condor_max_spread_pct_wing: float = Field(default=0.15, gt=0, alias="CONDOR_MAX_SPREAD_PCT_WING")
    condor_spread_abs_floor: float = Field(default=0.30, ge=0, alias="CONDOR_SPREAD_ABS_FLOOR")
    condor_min_pop: float = Field(default=0.60, gt=0, lt=1, alias="CONDOR_MIN_POP")
    condor_max_loss_per_trade: float = Field(default=15_000, gt=0, alias="CONDOR_MAX_LOSS_PER_TRADE")
    # Margin shown as width*qty*factor - credit, LABELLED estimate. Review
    # catch: factor 1.0 reduces to bare max loss, which understates real NSE
    # SPAN+exposure margin for a hedged condor ~2-4x — a trader capital-
    # planning off the card would get a rejected order. 2.0 lands in the
    # observed ballpark; the Kite basket_order_margins API is the accuracy
    # upgrade when it matters.
    condor_margin_factor: float = Field(default=2.0, gt=0, alias="CONDOR_MARGIN_FACTOR")
    condor_sl_mult: float = Field(default=1.75, gt=1, alias="CONDOR_SL_MULT")
    condor_profit_target_pct: float = Field(default=50, gt=0, le=100, alias="CONDOR_PROFIT_TARGET_PCT")
    condor_max_adjustments: int = Field(default=1, ge=0, alias="CONDOR_MAX_ADJUSTMENTS")
    condor_adj_min_credit: float = Field(default=8, ge=0, alias="CONDOR_ADJ_MIN_CREDIT")
    condor_entry_from: str = Field(default="10:00", alias="CONDOR_ENTRY_FROM")
    condor_entry_to: str = Field(default="14:30", alias="CONDOR_ENTRY_TO")
    # Expiry-day hard exit deadline. CAS freezes NIFTY 15:15-15:35 (live since
    # 03-Aug-2026) — the last clean exit is well before the close print.
    condor_expiry_exit_ist: str = Field(default="14:30", alias="CONDOR_EXPIRY_EXIT_IST")
    condor_event_window_h: float = Field(default=24.0, ge=0, alias="CONDOR_EVENT_WINDOW_H")
    condor_snapshot_s: float = Field(default=300.0, gt=0, alias="CONDOR_SNAPSHOT_S")
    condor_snapshot_keep_days: int = Field(default=180, ge=1, alias="CONDOR_SNAPSHOT_KEEP_DAYS")

    # --- R&D tab (docs/rnd-tab-plan-2026-08-12.md) ---
    # Windows for the "+5% after entry" question, per mode; positional is
    # additionally capped at a fixed IST time on the entry day. Touch-ladder
    # LEVELS are frozen in trades/monitor.py, deliberately not here.
    rnd_window_scalp_min: int = Field(default=30, ge=1, alias="RND_WINDOW_SCALP_MIN")
    rnd_window_intraday_min: int = Field(default=120, ge=1, alias="RND_WINDOW_INTRADAY_MIN")
    rnd_window_positional_min: int = Field(default=240, ge=1, alias="RND_WINDOW_POSITIONAL_MIN")
    rnd_positional_cutoff_ist: str = Field(default="14:50", alias="RND_POSITIONAL_CUTOFF_IST")
    rnd_min_sample: int = Field(default=30, ge=1, alias="RND_MIN_SAMPLE")
    # R3 policy ledgers: four pre-registered exit-policy twins per clean
    # paper fill (P-5W / P-5T / P-LAD + its 2-lot baseline), graded at 30+
    # diverged pairs. OFF by default — the paper suite's row-count
    # expectations predate the twins; the deliberate enable lives in .env,
    # same pattern as CONDOR_ENABLED.
    rnd_policy_ledgers: bool = Field(default=False, alias="RND_POLICY_LEDGERS")

    # --- Closing Day Strategy tab (app/closing) ---
    # A research surface only: it places no orders and emits no signals, so
    # there is no enable flag to forget. Almost every number the study needs
    # (carry, the vol term structure, the ATM bid-ask) is FITTED from real
    # quotes rather than configured — the two knobs below are the ones a
    # reader might legitimately want to change.
    closing_lots: int = Field(default=1, ge=1, alias="CLOSING_LOTS")
    closing_lot_size: int = Field(default=65, ge=1, alias="CLOSING_LOT_SIZE")
    # NIFTY weekly expiry moved Thursday -> Tuesday with effect from
    # 2025-09-02. Nothing in this repo can verify the pre-switch leg, so it is
    # stated as configuration rather than buried as a constant.
    closing_expiry_switch: str = Field(default="2025-09-02", alias="CLOSING_EXPIRY_SWITCH")
    # Which 15:00 reference decides CE vs PE: "day_open" (default) or
    # "prev_close" (the original hypothesis). Not a tuning knob — the two are
    # different hypotheses, and the tab reports both side by side regardless.
    # day_open is the default because prev_close is barely separable from an
    # always-buy-CE control; see docs/closing-day-strategy-2026-08-19.md.
    closing_signal_mode: str = Field(default="day_open", alias="CLOSING_SIGNAL_MODE")
    # Morning auto-grade: once the 09:50 exit print has settled on a trading
    # day, the ops loop syncs and re-runs the closing + overnight analyses so
    # yesterday's night lands in the ledgers (and grades the live 3pm card)
    # without a manual click. Advisory analytics only — no orders, no pushes.
    closing_auto_grade: bool = Field(default=True, alias="CLOSING_AUTO_GRADE")

    @property
    def condor_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.condor_symbols.split(",") if s.strip()]

    @property
    def condor_wing_width_list(self) -> list[int]:
        out = []
        for w in self.condor_wing_widths.split(","):
            try:
                out.append(int(w.strip()))
            except ValueError:
                continue
        return sorted(out)

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
