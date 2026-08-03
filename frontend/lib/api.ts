// REST + type layer for the Tradewell backend.
// Types mirror backend/app/models/schemas.py — keep them in sync.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
export const WS_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws";

export type Timeframe = "1m" | "3m" | "5m" | "15m";

export interface Candle {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface IndicatorSnapshot {
  vwap: number | null;
  ema9: number | null;
  ema20: number | null;
  ema50: number | null;
  rsi: number | null;
  atr: number | null;
  adx: number | null;
  supertrend: number | null;
  supertrend_dir: "up" | "down" | null;
  bb_width: number | null;
  prev_day_high: number | null;
  prev_day_low: number | null;
}

export interface UnderlyingSnapshot {
  symbol: string;
  tradingsymbol: string;
  instrument_token: number;
  ltp: number | null;
  change: number | null;
  change_pct: number | null;
  day_open: number | null;
  day_high: number | null;
  day_low: number | null;
  prev_close: number | null;
  updated_at: number | null;
  fut_token: number | null;
  fut_ltp: number | null;
  fut_tradingsymbol: string | null;
}

export interface VixSnapshot {
  ltp: number | null;
  change_pct: number | null;
  status: string | null;
}

export interface MarketSnapshot {
  server_time: number;
  market_open: boolean;
  underlyings: UnderlyingSnapshot[];
  vix: VixSnapshot | null;
  last_tick_age: number | null;
  feed_stale: boolean;
  /** True only when this feed start verified the alert webhook with a real push. */
  alerts_armed: boolean;
  /** Shared guest topic (ALERT_WEBHOOK_URL_2): null = none configured; true/false
   *  = its startup verification push did/didn't land. */
  guest_alerts_armed: boolean | null;
}

export interface OptionRow {
  strike: number;
  ce_token: number | null;
  ce_ltp: number | null;
  ce_oi: number | null;
  ce_oi_change: number | null;
  ce_volume: number | null;
  ce_iv: number | null;
  pe_token: number | null;
  pe_ltp: number | null;
  pe_oi: number | null;
  pe_oi_change: number | null;
  pe_volume: number | null;
  pe_iv: number | null;
}

export interface OptionChain {
  symbol: string;
  expiry: string | null;
  atm_strike: number | null;
  pcr: number | null;
  rows: OptionRow[];
  updated_at: number | null;
}

// ---- Signal engine (Phase 2) ----
export type TradingMode = "intraday" | "positional" | "scalp";
export type Bias = "bullish" | "bearish" | "neutral";
export type SignalAction = "buy_ce" | "buy_pe" | "wait" | "avoid";
export type SignalState = "active" | "expired" | "cancelled" | "invalidated";

export interface ScoreComponent {
  name: string;
  points: number;
  max: number;
  reasons: string[];
}

export interface ScoreBreakdown {
  direction: "CE" | "PE" | "NONE";
  components: ScoreComponent[];
  total: number;
  max: number;
}

export interface MarketStatus {
  symbol: string;
  mode: TradingMode;
  regime: string;
  regime_label: string;
  bias: Bias;
  bull_score: number;
  bear_score: number;
  headline: string;
  vix_status: string | null;
  news_label: string | null;
  news_net: number | null;
  notes: string[];
}

// ---- News intelligence (Phase 4) ----
export interface AnalyzedNews {
  id: string;
  title: string;
  link: string;
  source: string;
  published: number;
  analyzed_at: number;
  sentiment: "positive" | "negative" | "neutral";
  affected_market: "NIFTY" | "BANKNIFTY" | "FINNIFTY" | "BROAD" | "OTHER";
  impact_score: number;
  impact_duration: string;
  event_type: string;
  confidence: number;
  is_market_moving: boolean;
  summary: string;
}

export interface NewsSentiment {
  symbol: string;
  net_score: number;
  label: string;
  items_considered: number;
  top_headlines: string[];
  updated_at: number | null;
}

export interface NewsResponse {
  enabled: boolean;
  updated_at: number | null;
  sentiments: NewsSentiment[];
  items: AnalyzedNews[];
  /** High-impact market-moving headlines the dashboard alerts on. */
  breaking: AnalyzedNews[];
  note: string | null;
}

// ---- Market Mood Index (Tickertape) — read-only context gauge ----
export interface MarketMood {
  value: number;               // 0-100
  zone: "Extreme Fear" | "Fear" | "Greed" | "Extreme Greed";
  nifty: number | null;
  date: string | null;
  updated_at: number;
  source: string;
}

export interface SignalCard {
  id: string;
  symbol: string;
  mode: TradingMode;
  title: string;
  action: SignalAction;
  direction: "CE" | "PE" | "NONE";
  state: SignalState;
  contract: string;
  strike: number;
  expiry: string | null;
  entry_low: number;
  entry_high: number;
  premium_sl: number;
  /** Backstop that actually ends the trade when the index invalidation is primary. */
  disaster_sl: number | null;
  /** Early partial-book level: book half here, stop moves to entry. */
  quick_target: number | null;
  target1: number;
  target2: number;
  trailing_sl_rule: string;
  risk_reward: number;
  confidence: number;
  underlying_invalidation: string;
  invalidation_note: string;
  reasons: string[];
  created_at: number;
  valid_until: number;
  score: ScoreBreakdown;
  ref_spot: number | null;
  ref_entry_premium: number | null;
  /** When the ladder was last re-priced; null = never. Freshness reads this over created_at. */
  repriced_at: number | null;
  /** Live LTP of this card's option, fresh at request time; null if no tick. */
  live_premium: number | null;
  /** Lots implied by TRADING_CAPITAL × RISK_PER_TRADE_PCT; null when unconfigured. */
  suggested_lots: number | null;
  sizing_note: string | null;
  /**
   * Affordability prefill from the runtime-editable trading fund: how many
   * whole lots the fund buys at the freshest premium. Tracks live_premium on
   * each poll. Null when the fund is unset or no premium/lot size is known.
   */
  fund_lots: number | null;
  fund_qty: number | null;
  fund_note: string | null;
  /** Scheduled macro event nearby (from .events.json); sizing is halved while set. */
  event_note: string | null;
  /** Contract multiplier — required to turn premium levels into rupees. */
  lot_size: number | null;
  trading_capital: number | null;
  daily_loss_limit: number | null;
}

export interface SignalResponse {
  symbol: string;
  mode: TradingMode;
  evaluated_at: number;
  status: MarketStatus;
  action: SignalAction;
  signal: SignalCard | null;
  no_trade_reason: string | null;
  score: ScoreBreakdown | null;
}

/** One evaluation (~5s) of both directional scores — the trend behind the number. */
export interface ScoreHistoryPoint {
  ts: number;
  bull: number;
  bear: number;
  direction: "CE" | "PE" | null;
  /** Component points of the best direction at that moment, keyed by name. */
  components: Record<string, number>;
}

/** A card the engine issued — including the ones that came and went unseen. */
export interface SignalHistoryRow {
  id: string;
  mode: TradingMode;
  direction: "CE" | "PE";
  contract: string;
  /** 0-100 confidence at issue. Missing/null means "not recorded" (a row from
   *  a backend that predates score passthrough) — render nothing, never 0. */
  score?: number | null;
  title?: string | null;
  state: SignalState;
  /** Whether you acted on it: journal, paper book, both, or not at all. */
  taken: "live" | "paper" | "both" | null;
  created_at: number;
  valid_until: number;
  entry_low: number;
  entry_high: number;
  premium_sl: number;
  target1: number;
  target2: number;
  ref_entry_premium: number | null;
}

export interface RepriceResult {
  status: "repriced" | "closed";
  signal: SignalCard | null;
  score: number | null;
  score_needed: number | null;
  reason: string | null;
}

export interface AuthStatus {
  authenticated: boolean;
  api_key_configured: boolean;
  login_url: string | null;
  user_id: string | null;
  ticker_connected: boolean;
  instruments_loaded: boolean;
  message: string | null;
}

// ---- Trades (Phase 3) ----
export type TradeStatus = "entered" | "partial" | "exited" | "ignored";
export type TradeAction =
  | "hold" | "book_partial" | "move_sl_entry" | "trail_sl" | "exit"
  | "target1_reached" | "target2_reached" | "stop_loss_hit" | "invalidated" | "time_exit"
  | "stall_exit";

export interface TradeEvent {
  ts: number;
  kind: string;
  note: string;
}

export interface Trade {
  id: string;
  signal_id: string | null;
  symbol: string;
  mode: TradingMode;
  direction: "CE" | "PE" | "NONE";
  contract: string;
  strike: number;
  expiry: string | null;
  token: number | null;
  entry_premium: number;
  lots: number;
  lot_size: number;
  quantity: number;
  /** Size at entry — `quantity` shrinks on a partial, so this is the return base. */
  initial_quantity: number | null;
  status: TradeStatus;
  stop_loss: number;
  target1: number;
  target2: number;
  trailing_sl: number;
  quick_target: number | null;
  t0_hit: boolean;
  invalidation_level: number | null;
  invalidation_dir: string | null;
  /** Sticky invalidation: fired latches the break; ack newer than fired = acknowledged. */
  invalidation_fired_at: number | null;
  invalidation_ack_at: number | null;
  current_premium: number | null;
  pnl: number | null;
  pnl_pct: number | null;
  recommendation: TradeAction;
  recommendation_note: string | null;
  t1_hit: boolean;
  created_at: number;
  entered_at: number;
  exited_at: number | null;
  exit_premium: number | null;
  realized_pnl: number;
  /** Tradewell closed this row on a plan trigger — not a fill you reported. */
  auto_closed: boolean;
  auto_close_reason: string | null;
  /** "estimated" | "broker" | "simulated" — what the exit price actually is. */
  exit_price_source: string | null;
  /** Why the trade ended, in your words — feeds the exit-type expectancy report. */
  exit_reason: string | null;
  /** Quantity Zerodha's position book last reported; null = never confirmed. */
  broker_qty: number | null;
  broker_checked_at: number | null;
  notes: string | null;
  events: TradeEvent[];
}

// ---- Market pulse (live tape analytics under the score card) ----
export interface MarketPulse {
  symbol: string;
  updated_at: number;
  fut_ltp?: number;
  day_high?: number;
  day_low?: number;
  /** Where the future trades inside today's range, 0 (low) to 100 (high). */
  range_pos_pct?: number;
  vwap?: number;
  atr?: number;
  /** Signed distance from VWAP in ATR units — stretch, not direction quality. */
  vwap_dist_atr?: number;
  /** Mean 15m volume today vs prior sessions in the frame. 1 = normal. */
  vol_run_rate?: number;
  /** Today's high-low as a % of the typical prior daily range. */
  range_vs_typical_pct?: number;
  pcr?: number;
  pcr_first?: number;
  /** PCR drift since Tradewell first observed it today (not the exchange open). */
  pcr_shift?: number;
  vix?: number;
  vix_chg_pct?: number;
  /** Plain-language read of the numbers above — deterministic rules, no AI. */
  story?: string;
}

// ---- Paper trading (simulated; no orders) ----
export interface PaperRow {
  id: string;
  contract: string;
  direction: "CE" | "PE" | "NONE";
  entered_at: number;
  exited_at: number | null;
  entry: number;
  exit: number;
  quantity: number;
  reason: string | null;
  gross_pnl: number;
  charges: number;
  net_pnl: number;
  return_pct: number;
  /** "honest" or "inflated (pre-honest-fill)" — pre-23-Jul fills were fictional. */
  era?: string;
  /** Fill of a card the volume/OI floor vetoed — counterfactual, not evidence. */
  hollow?: boolean;
  /** Which shadow ledger a hollow row belongs to: the participation-floor
      hypothesis, the 14:15 late-cutoff hypothesis, or the stop-basis A/B twin. */
  shadow_class?: "floor" | "late" | "refire" | "stopb" | null;
  mode?: string;
}

export interface PaperSummary {
  trades: number;
  open: number;
  wins: number;
  losses: number;
  win_rate: number;
  gross_pnl: number;
  charges: number;
  net_pnl: number;
  avg_win: number;
  avg_loss: number;
  expectancy: number;
  by_reason: Record<string, number>;
  /** Net-of-charges expectancy split by trading mode — the go-live/gate number. */
  by_mode?: Record<string, { trades: number; net_pnl: number; expectancy: number; win_rate: number }>;
  note: string;
  /** Net verdict on the cards the volume/OI floor vetoed; null until one fills. */
  hollow?: { trades: number; open: number; net_pnl: number; expectancy: number; win_rate: number } | null;
  /** The 14:15-cutoff hypothesis ledger: fills of late-vetoed cards (14:15-15:10).
      Positive expectancy at 30+ fills retires the cutoff. */
  late_shadow?: { trades: number; open: number; net_pnl: number; expectancy: number; win_rate: number } | null;
  /** The re-fire guard's hypothesis ledger: fills of cards the guard refused.
      Positive expectancy at 30+ fills shortens or retires the guard. */
  refire_shadow?: { trades: number; open: number; net_pnl: number; expectancy: number; win_rate: number } | null;
  /** 1-lot exit-policy A/B: trailing ratchet vs banking the whole position at
      the quick target — a paired counterfactual on the same recorded fills. */
  exit_ab?: {
    policy_live: "ratchet" | "quick_bank";
    n: number;
    n_diverged?: number;
    /** Fills the live banking trigger closed in an earlier flag-on period — in neither arm. */
    banked_live_excluded?: number;
    ratchet?: { net_pnl: number; expectancy: number; win_rate: number };
    quick_bank?: { net_pnl: number; expectancy: number; win_rate: number };
    delta_net?: number;
    verdict?: string;
    note?: string;
  } | null;
  /** Stop-basis paired A/B (audit P1-5): every clean fill's twin runs the
      OTHER stop basis. Verdict at 30+ diverged pairs — STOP_PRIMARY was
      flipped twice on single-trade evidence. */
  stop_ab?: {
    n: number;
    n_diverged: number;
    /** Pairs with either leg still open — not yet settled. */
    pending: number;
    premium_stop?: { net_pnl: number; expectancy: number; win_rate: number };
    underlying_stop?: { net_pnl: number; expectancy: number; win_rate: number };
    delta_net?: number;
    verdict?: string;
  } | null;
  /** Positional rows that held through an IST day boundary — the overnight-gap
      bet graded, with the deliberate late-day (≥14:30) entries split out. */
  overnight?: {
    trades: number;
    net_pnl: number;
    expectancy: number;
    win_rate: number;
    evening: { trades: number; net_pnl: number; expectancy: number; win_rate: number } | null;
    rows: {
      contract: string;
      direction: string;
      /** Card score at fill; null on rows recorded before the field existed. */
      score: number | null;
      entered_at: number;
      exited_at: number;
      evening: boolean;
      entry: number;
      /** First premium print of the next session; null on pre-latch rows. */
      next_open: number | null;
      overnight_move_pct: number | null;
      net_pnl: number;
      reason: string | null;
    }[];
  } | null;
  rows: PaperRow[];
  slippage_pct: number;
  lots: number;
}

// ---- Backtest (Phase 5) ----
export interface BacktestTrade {
  entry_ts: number;
  exit_ts: number;
  direction: "CE" | "PE" | "NONE";
  regime: string;
  score: number;
  entry: number;
  stop: number;
  target: number;
  exit: number;
  r_multiple: number;
  outcome: "target" | "stop" | "time" | "eod";
}

export interface BacktestResult {
  symbol: string;
  mode: TradingMode;
  timeframe: string;
  from_ts: number;
  to_ts: number;
  bars: number;
  trades_total: number;
  wins: number;
  losses: number;
  win_rate: number;
  expectancy_r: number;
  avg_win_r: number;
  avg_loss_r: number;
  profit_factor: number | null;
  max_drawdown_r: number;
  total_r: number;
  ce_trades: number;
  ce_win_rate: number;
  pe_trades: number;
  pe_win_rate: number;
  equity_curve: number[];
  trades: BacktestTrade[];
  note: string;
}

// ---- Patterns Module (standalone research: 3y NIFTY 5-min) ----
export interface PatternsStatus {
  bars: number;
  last_sync: string | null;
  results_available: boolean;
  results_generated_at: string | null;
}

export interface DayOfWeekStats {
  n_days: number;
  open_to_close_bps: { mean: number; median: number };
  up_days: number;
  up_day_rate: number;
  gap_bps: { mean: number; abs_mean: number };
  gap_up_days: number;
  gap_up_faded_rate: number | null;
  gap_down_days: number;
  gap_down_faded_rate: number | null;
  avg_range_bps: number;
  first_hour_range_share: number;
  trend_day_rate: number;
}

export interface TimeOfDaySlot {
  slot: string;
  n_days: number;
  mean_ret_bps: number;
  up_rate: number;
  mean_abs_ret_bps: number;
  vol_proxy_share: number | null;
}

export interface PatternOutcome {
  count: number;
  direction?: "bullish" | "bearish" | "neutral";
  n_scored?: number;
  hit_rate_30m?: number;
  avg_fwd_30m_bps?: number;
}

export interface CandleFrequency {
  overall: PatternOutcome & { direction: "bullish" | "bearish" | "neutral" };
  by_weekday: Record<string, PatternOutcome>;
}

export interface LevelRow {
  level: number;
  days_touched: number;
  total_touches: number;
  as_resistance: number;
  as_support: number;
  held: number;
  broke: number;
  hold_rate: number;
  first_touch: string;
  last_touch: string;
}

/** One historical outcome cell: n, hit rate in the pattern's direction, move size. */
export interface ConditionalCell {
  n: number;
  hit_rate: number;
  avg_bps: number;
  median_bps: number;
  /** "tendency" (n≥30 outside 45–55%), "coin", or "sample too small". */
  verdict?: string;
}

export interface ConditionalHorizon {
  all: ConditionalCell;
  by_volume?: { high?: ConditionalCell; normal?: ConditionalCell; low?: ConditionalCell };
  /** High-volume hit minus low-volume hit, percentage points (both n≥30 only). */
  volume_effect_pp?: number;
}

export interface ConditionalOutcome {
  direction: "bullish" | "bearish";
  n_total: number;
  horizons: Record<string, ConditionalHorizon>; // "15m" | "30m" | "60m"
}

/** Today's tape joined to the 3y conditional table — frequencies, not forecasts. */
export interface PatternsLiveRead {
  bars_today: number;
  last_bar?: string;
  last_close?: number;
  patterns: {
    bar: string;
    pattern: string;
    direction: "bullish" | "bearish";
    volume_regime: "high" | "normal" | "low" | null;
    historical_30m: ConditionalCell | null;
    historical_30m_all: ConditionalCell | null;
    conditioned: boolean;
    horizons: Record<string, ConditionalCell> | null;
  }[];
  volume: {
    current_regime: "high" | "normal" | "low" | null;
    pace_vs_typical: number | null;
    pace_days: number | null;
    trend: "rising" | "falling" | "flat" | null;
    /** Actual window the trend compares (shrinks early in the session). */
    trend_window_min: number | null;
  } | null;
  note: string;
}

/** A level-touch callout fired by the live watch (backend level_watch.py). */
export interface LevelAlert {
  ts: number;
  side: "buy" | "sell";
  level: number;
  spot: number;
  hold_rate: number | null;
  days_touched: number | null;
  strike: number;
  expiry: string | null;
  ce_ltp: number | null;
  title: string;
}

export interface LevelAlertsResponse {
  enabled: boolean;
  spot?: number | null;
  /** Strong levels currently on watch (index-space; add basis for futures charts). */
  watched: LevelRow[];
  alerts: LevelAlert[];
}

export interface PatternsResults {
  disclaimer: string;
  generated_at: string;
  data: {
    bars: number;
    days: number;
    from: string;
    to: string;
    last_close: number;
    volume_note: string;
    has_volume_proxy: boolean;
  };
  day_of_week: Record<string, DayOfWeekStats>;
  time_of_day: Record<string, TimeOfDaySlot[]>;
  candlestick_frequency: Record<string, CandleFrequency>;
  /** Volume-conditioned pattern outcomes (15/30/60m) — absent until the first
      re-analyze after the tendencies layer shipped. */
  conditional_outcomes?: Record<string, ConditionalOutcome>;
  volume_pace?: { days: number; cum_median: number[] };
  levels: {
    method: string;
    pivot_count: number;
    levels: LevelRow[];
    round_number_stats: Record<
      string,
      { pivots_at_round: number; share: number; expected_share_if_random: number }
    >;
  };
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) {
    // Keep the "<status> <statusText>" prefix — several callers branch on it —
    // but carry the backend's detail so error copy can be precise (review
    // catch: a 409 "re-analyze" and a 404 "no results" read identically
    // without it), and the typed status rides on ApiError.
    const body = await res.json().catch(() => ({}));
    const detail = typeof body?.detail === "string" ? `: ${body.detail}` : "";
    throw new ApiError(`${res.status} ${res.statusText} — ${path}${detail}`, res.status);
  }
  return res.json() as Promise<T>;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const d = body?.detail;
    // FastAPI validation errors arrive as an array of {loc,msg,...}. A refusal
    // the UI can offer a way THROUGH — an oversize entry, say — arrives as an
    // object carrying a `code`, so the caller can branch on it.
    const msg = Array.isArray(d)
      ? d.map((e) => e?.msg ?? JSON.stringify(e)).join("; ")
      : typeof d === "string"
        ? d
        : typeof d?.message === "string"
          ? d.message
          : `${res.status} — ${path}`;
    throw new ApiError(msg, res.status, typeof d?.code === "string" ? d.code : undefined);
  }
  return res.json() as Promise<T>;
}

/** An HTTP failure that kept its status and machine-readable code. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const api = {
  authStatus: () => getJSON<AuthStatus>("/auth/status"),
  createSession: async (request_token: string): Promise<AuthStatus> => {
    const res = await fetch(`${API_BASE}/auth/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_token }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail ?? `Session failed (${res.status})`);
    }
    return res.json();
  },
  candles: (symbol: string, tf: Timeframe) =>
    getJSON<Candle[]>(`/market/${symbol}/candles?tf=${tf}&limit=240`),
  indicators: (symbol: string, tf: Timeframe) =>
    getJSON<IndicatorSnapshot>(`/market/${symbol}/indicators?tf=${tf}`),
  optionChain: (symbol: string) => getJSON<OptionChain>(`/options/${symbol}`),
  signal: (symbol: string, mode: TradingMode = "intraday") =>
    getJSON<SignalResponse>(`/signals/${symbol}?mode=${mode}`),
  modes: () => getJSON<{ modes: TradingMode[] }>("/signals/modes"),
  pulse: (symbol: string) => getJSON<MarketPulse>(`/market/${symbol}/pulse`),

  news: () => getJSON<NewsResponse>("/news"),
  marketMood: () => getJSON<MarketMood | null>("/market/mood"),
  trades: () => getJSON<Trade[]>("/trades"),
  // `acknowledge_oversize` clears the one-time refusal when lots exceed the
  // card's suggestion; send it only after the trader has seen the rupee risk.
  enterTrade: (
    symbol: string, mode: TradingMode, lots: number, entry_premium?: number,
    signal_id?: string, acknowledge_oversize = false,
  ) =>
    postJSON<Trade>("/trades/enter", {
      symbol, mode, lots, entry_premium, signal_id, acknowledge_oversize,
    }),
  restartFeed: () => postJSON<AuthStatus>("/auth/feed/restart", {}),
  /** Full backend PROCESS restart (fresh code + fresh ticker) — not just the feed. */
  restartBackend: () => postJSON<{ restarting: boolean; note: string }>("/system/restart", {}),
  paperSummary: () => getJSON<PaperSummary>("/paper/summary"),
  reopenTrade: (tid: string) => postJSON<Trade>(`/trades/${tid}/reopen`, {}),
  ackInvalidation: (tid: string) => postJSON<Trade>(`/trades/${tid}/ack-invalidation`, {}),
  exitReason: (tid: string, reason: string) =>
    postJSON<Trade>(`/trades/${tid}/exit-reason`, { reason }),
  repriceSignal: (symbol: string, mode: TradingMode) =>
    postJSON<RepriceResult>(`/signals/${symbol}/reprice?mode=${mode}`, {}),
  scoreHistory: (symbol: string, mode: TradingMode, minutes = 120) =>
    getJSON<{ points: ScoreHistoryPoint[]; count: number }>(
      `/signals/${symbol}/score-history?mode=${mode}&minutes=${minutes}`,
    ),
  signalHistory: async (symbol: string, days = 7) => {
    try {
      return await getJSON<{ rows: SignalHistoryRow[]; count: number }>(
        `/signals/${symbol}/archive?mode=all&days=${days}`,
      );
    } catch {
      // Backend predates the archive endpoint (deploy lands at its next
      // restart) — degrade to the day-scoped history instead of erroring.
      return await getJSON<{ rows: SignalHistoryRow[]; count: number }>(
        `/signals/${symbol}/history?mode=all`,
      );
    }
  },
  exitTrade: (id: string, exit_premium?: number) =>
    postJSON<Trade>(`/trades/${id}/exit`, { exit_premium }),
  partialTrade: (id: string, exit_premium?: number, fraction = 0.5) =>
    postJSON<Trade>(`/trades/${id}/partial`, { exit_premium, fraction }),
  ignoreTrade: (id: string) => postJSON<Trade>(`/trades/${id}/ignore`, {}),

  backtest: (symbol: string, mode: TradingMode, days: number) =>
    postJSON<BacktestResult>("/backtest", { symbol, mode, days }),

  patternsStatus: () => getJSON<PatternsStatus>("/patterns/status"),
  /** 404s until the first analyze — callers treat that as "no results yet". */
  patternsResults: () => getJSON<PatternsResults>("/patterns/results"),
  patternsSync: (years = 3) => postJSON<{ total_bars: number }>(`/patterns/sync?years=${years}`, {}),
  patternsAnalyze: () => postJSON<PatternsResults>("/patterns/analyze", {}),
  patternsLiveRead: () => getJSON<PatternsLiveRead>("/patterns/live-read"),
  levelAlerts: () => getJSON<LevelAlertsResponse>("/patterns/level-alerts"),
};
