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
export type TradingMode = "intraday" | "positional";
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
  score: number;
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
  /** Quantity Zerodha's position book last reported; null = never confirmed. */
  broker_qty: number | null;
  broker_checked_at: number | null;
  notes: string | null;
  events: TradeEvent[];
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
  note: string;
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

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
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
  paperSummary: () => getJSON<PaperSummary>("/paper/summary"),
  reopenTrade: (tid: string) => postJSON<Trade>(`/trades/${tid}/reopen`, {}),
  ackInvalidation: (tid: string) => postJSON<Trade>(`/trades/${tid}/ack-invalidation`, {}),
  repriceSignal: (symbol: string, mode: TradingMode) =>
    postJSON<RepriceResult>(`/signals/${symbol}/reprice?mode=${mode}`, {}),
  scoreHistory: (symbol: string, mode: TradingMode, minutes = 120) =>
    getJSON<{ points: ScoreHistoryPoint[]; count: number }>(
      `/signals/${symbol}/score-history?mode=${mode}&minutes=${minutes}`,
    ),
  signalHistory: (symbol: string) =>
    getJSON<{ rows: SignalHistoryRow[]; count: number }>(`/signals/${symbol}/history?mode=all`),
  exitTrade: (id: string, exit_premium?: number) =>
    postJSON<Trade>(`/trades/${id}/exit`, { exit_premium }),
  partialTrade: (id: string, exit_premium?: number, fraction = 0.5) =>
    postJSON<Trade>(`/trades/${id}/partial`, { exit_premium, fraction }),
  ignoreTrade: (id: string) => postJSON<Trade>(`/trades/${id}/ignore`, {}),

  backtest: (symbol: string, mode: TradingMode, days: number) =>
    postJSON<BacktestResult>("/backtest", { symbol, mode, days }),
};
