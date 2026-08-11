# Iron Condor Module — Phase 1 Audit (11-Aug-2026)

**Scope.** Full-codebase audit ahead of the proposed IRON CONDOR tab: what exists, what's
reusable, what data is missing, where the current infrastructure actively fights a 4-leg
short-premium strategy, and the architecture proposed for Phase 2/3. Six parallel
subsystem audits (core, options, market-data, signal engine, frontend, trades/backtest)
plus the two prior audit docs (30-Jul deep audit, 10-Aug methodology audit).
**No production code was changed by this audit.**

---

## 1. Current architecture relevant to this feature

**Stack:** FastAPI (`app/main.py:97`) + uvicorn on Python 3.9, Next.js 14 / Tailwind
frontend, Kite Connect for all market data. Advisory-only: the app never places orders
(`routes_kite_basket.py:192` hard-codes BUY; `/kite/protect` exists specifically to
prevent accidental shorts). Persistence is in-memory `MarketState` + atomic dot-JSON
stores + append-only JSONL archives + one SQLite DB (`.patterns_candles.db`). No
database by decision; TimescaleDB/Redis provisioned but unused.

**Data path:** KiteTicker FULL-mode WS → `MarketState.ticks` → consumers. No REST quote
polling anywhere. Subscribed universe per symbol: spot + near-month future + CE/PE for
**nearest** and **monthly** expiries, ATM ± 30 strikes (61 strikes/expiry,
`instruments.py:184`). Option chain rebuilt every 5 s from the tick cache
(`OptionChainBuilder`, `options/chain.py`): LTP, OI, OI-change vs session baseline,
volume, IV. Bid/ask depth arrives in FULL ticks but is **dropped by the chain**; only
`strike._spread_ok` reads `state.ticks[token]["depth"]` directly.

**Loops (feed-lifetime, `services.py:320-334`):** option chain 5 s · signal eval 5 s ·
trade monitor 5 s · paper 5 s · broker reconcile 15 s · news 60 s. App-lifetime: WS
broadcaster 1 s (single `MarketSnapshot` message type), supervisor 60 s, watchdog 30 s.

**Signal engine (directional, the incumbent):** ticks → 3m/15m closed candles →
indicators → `regime.classify` (7-input directional vote) → dual 0–100 scoring → gate
78/70 → ~20 ordered gates (extension, strike ATM±1, premium freshness 120 s,
invalidation room, integrity vetoes, measured vetoes, WATCH→CONFIRM) → throttle → one
card per (symbol, mode) slot → monitor → archive. Six shadow-ledger classes trial every
new hypothesis behind the paper book with a 30-fill verdict rule.

**Options math today:** `options/iv.py` — Black-Scholes price + bisection implied vol
(`RISK_FREE = 0.065`, returns percentage), `years_to_expiry` anchored to 15:30 IST
settlement. IV solved only within **±5 strikes of ATM** (`chain.py:28`).
**No Greeks exist anywhere in the codebase** — delta/gamma/theta/vega are never
computed; "delta" appears only as prose rationale (`strike.py:93`, `modes.py:99`).

**Frontend:** 3 routes (`/` Pulse, `/patterns`, `/opening`) + `ModuleSwitcher` pill nav;
`usePolling` (REST) + one WS hook used once; dark-only design tokens
(`tailwind.config.ts`), `.card`/`.tag` primitives; `SignalPanel.tsx` is the card
template; `lightweight-charts` for candles, hand-rolled SVG for sparklines/equity
curves; `lib/tradeMath.ts` documents the invariant "every Tradewell signal is a BOUGHT
option… adding a sign flip would be a bug."

---

## 2. Existing components we can reuse

| Component | Where | Reuse for Iron Condor |
|---|---|---|
| Option universes, ATM±30 subscription | `kite/instruments.py:130-299` | Covers condor legs: NIFTY ±1,500 pts — ample for 0.10–0.25Δ shorts + wings, both expiries |
| BS price / implied-vol / T-years | `options/iv.py` | The pricing core. Closed-form Greeks are a small, pure extension of `bs_price` + `_norm_cdf` |
| Risk-neutral P(above) = N(d2) | `assistant.py:147` `_p_above` | The POP calculation, already written — needs productizing out of the chat assistant |
| India VIX: live tick, 370-day closes, percentile | `state.py:172-201`, `services.py:32` | Volatility regime + IV-rank proxy until real IV history accumulates |
| Indicators (VWAP, EMA 9/20/50, RSI, ATR, ADX, Supertrend, BB-width) on 1/3/5/15m | `market/indicators.py`, `candles.py` | Regime + breakout-risk inputs; 15m frame survives rollover (~24 trading days of context) |
| Regime vote machinery (7-fact `net`, ADX floors, BB-width compression, ATR-spike) | `signals/regime.py` | The *inputs* are right for a range classifier; the classifier itself is not reusable as-is (§5) |
| OI walls, put/call writing, PCR | `signals/features.py:94-151` `oi_analysis` | Chain-structure score + short-strike placement (resistance/support strikes) |
| S/R level engine | `patterns/levels.py` `find_levels` | Range-boundary evidence, richer than the 10-bar structure window |
| Shadow-ledger + archive pattern | `signals/store.py:411-443`, `archive.py` | The on-ramp: condor signals ship as a recorded ledger first, exactly like scalp/setup/confirm did |
| Eval-trace pattern | `signals/eval_trace.py` | Template for the condor decision trace (the "why no trade" dataset) |
| Premium freshness gate | `signals/engine.py:29` `premium_quote` | Per-leg staleness validation, verbatim |
| Event calendar + session/holiday calendar | `market/events.py`, `market/calendar.py` | Event filter + entry-window logic (see §4: `.events.json` currently absent) |
| Notifications (private + guest audiences) | `notify.py` | Condor alerts, unchanged |
| Charges model (Zerodha F&O) | `paper/charges.py`, `lib/tradeMath.ts` | Extend to 4 legs / 8 orders with sell-side STT; constants already correct |
| Frontend module-tab pattern | `ModuleSwitcher.tsx`, `app/opening/page.tsx` | `/condor` tab is a 3-file addition (page, Lab component, MODULES entry + activeKey ladder) |
| Option-chain table | `OptionChainTable.tsx` | Embed with leg highlighting |
| SVG chart idiom | `Spark` (`SignalPanel.tsx:46`), `EquityCurve` | The payoff diagram: compute points → one `<path>` — no new dependency |
| 3-year 5-min NIFTY spine + NIFTYBEES volume proxy | `patterns/store.py` (SQLite), `patterns/data.py` | Range-hold validation over history (underlying side of the backtest) |

---

## 3. Data already available

**Live (per 5 s cycle):** spot/future LTP · option LTP/OI/OIΔ/volume for 61 strikes ×
2 expiries × CE/PE · level-1 bid/ask in raw FULL ticks (not surfaced in chain rows) ·
IV for ATM±5 strikes · VIX (level, %-change, status band, 1-yr percentile) · candles +
indicators on 4 timeframes (near-month future) · PCR · OI walls · put/call writing ·
market structure · news sentiment (Claude-classified RSS) · per-contract lot sizes ·
session/holiday calendar (2026 only).

**Historical:** 3 y of NIFTY 5-min index OHLC + volume proxy (SQLite) · ≤90 d of
near-month futures candles via Kite historical API (fetched per backtest run) ·
370 d of VIX daily closes.

---

## 4. Data missing

Ranked by how much it constrains the module:

1. **Option-chain / IV / spread history — does not exist.** Per-strike IV is computed
   live and discarded; spreads are unlogged; OI history is not persisted. (The 10-Aug
   audit already flags this: "Log IV, OI, spread on every card… unlocks Phase-12
   questions forever.") Consequence: **a premium-true Iron Condor backtest is
   impossible today**, and IV percentile/rank cannot be computed. Snapshot logging must
   ship first, regardless of everything else.
2. **Greeks** — none. Needed: closed-form BS delta/gamma/theta/vega (pure functions on
   top of `iv.py`; no scipy needed, `_norm_cdf` exists; pdf is one line).
3. **Margin** — no `kite.margins()`/`basket_order_margins()` call, no SPAN estimate
   anywhere. Sizing is premium×lot (long-only logic). A short spread's margin
   (₹ hedged-basket per lot) is required for R:R, capital checks, and honest
   max-loss display. Options: Kite's `basket_order_margins` API (computes hedge
   benefit; read-only w.r.t. orders) vs a conservative width−credit approximation.
4. **Realized/historical volatility** — zero computation in the codebase (verified by
   grep). Close-to-close or Parkinson HV from the 15m frame or the 5-min spine is a
   handful of lines; needed for the IV-vs-RV leg of the volatility filter.
5. **ATM straddle / expected move** — not implemented. The assistant's `_implied_odds`
   computes an IV-based 1-sigma daily move as chat text only. Straddle price is one
   addition away (ATM CE + PE LTP are both in the chain).
6. **IV beyond ATM±5 strikes** — `_IV_DEPTH = 5` (±250 NIFTY pts) will frequently
   exclude 0.10–0.20Δ short strikes. Fix inside the condor module (solve IV on demand
   from ticks via `iv.implied_vol`) rather than touching the production chain.
7. **Bid/ask in chain rows** — plumbing gap, not a data gap (depth is in the ticks).
   Condor liquidity checks must read depth per leg; `_spread_ok` currently
   **fails open** on missing depth (`strike.py:36-45`) — condor legs must fail closed.
8. **"Next weekly" expiry** — only `nearest` and `monthly` universes exist. A third
   universe (+~122 tokens/symbol, well under Kite's 3,000 cap) if next-expiry condors
   are wanted.
9. **VIX intraday series** — VIX is subscribed but has no CandleEngine; "VIX rapidly
   expanding" needs a small intraday deque (session ticks are already flowing).
10. **`.events.json` is absent** — only the example file exists, so the entire
    scheduled-event caution path is silently inert (and the example lists a CPI print
    for 12-Aug). Needs recreating regardless of this module.
11. **POP** — computable from N(d2) (exists in assistant) once Greeks/IV plumbing is in
    place; nothing user-facing today.

---

## 5. Weaknesses / limitations in the current infrastructure

**Structural (things that actively fight a 4-leg short-premium strategy):**

- **Single-leg long-premium is a load-bearing assumption**, not a default: `Trade` has
  one contract/strike/token; `Direction` is `CE|PE` with no BUY/SELL axis; P&L is
  `(exit − entry) × qty` at five call sites; monitor stop semantics are "premium
  falls"; `charges(legs=2)` means round-trip-vs-lapse, not strategy legs;
  `tradeMath.ts` documents the invariant; the Kite basket is one BUY order by design.
  **The condor position layer must be new code, not a retrofit** — sign-flipping the
  existing path would be the exact bug the codebase warns about.
- **`SIDEWAYS` is a residual bucket, not a measured range** (`regime.py:111-118`): it
  fires on `|net| ≤ 1 OR adx < 16`, is stateless (no persistence), produces no range
  boundaries, takes no IV/premium input, and short-circuits as `tradeable=False`
  before any strike work (`engine.py:150-154`). A condor regime filter needs its own
  classifier that *measures* the range; it can reuse the inputs, not the labels.
- **No option history** (§4.1) — Phase 5 backtesting as specified (premium-true, by
  DTE/IV/credit) cannot be honest on current data. The 5-min spine supports only the
  underlying half ("did the range hold to expiry"); premium P&L would be
  BS-reconstructed synthetics and must be labelled as such — or deferred until logged
  snapshots accumulate, which is the house rule (30-Jul audit graded ORB and the
  fade the same way).

**Quality/operational (inherited by anything built on top):**

- Chain rows carry no per-row staleness; frozen ticks are republished with a fresh
  `updated_at` every 5 s; chains are never evicted after feed stop. PCR is computed
  over the ±10 displayed strikes and its basis shifts as ATM re-centres. `oi_change`
  is vs session baseline (schema comment says otherwise).
- Liquidity guards fail open (`_spread_ok`) or are silently bypassed (ATM fallback,
  flagged P0-3 in the 30-Jul audit).
- Score-calibration lesson: the 10-Aug audit shows the existing confidence score is
  **anti-calibrated above 80** and that stacked DEV-screened filters select noise
  (MODEL D kept 0 TEST trades). The condor score must be floors-first, components
  displayed, magnitude treated as a gate not a ranking — and pre-registered before
  tuning.
- Holiday calendar ends 25-Dec-2026 (fail-open); CAS regime (NIFTY frozen
  15:15–15:35 since 03-Aug) breaks close-window assumptions — expiry-day monitoring
  and settlement logic must be CAS-aware.
- Single-user, no API auth (loopback-bound); `/system/restart` is CSRF-reachable;
  `/ws` has no origin check. Not condor-specific but worth knowing.
- Frontend: module pages sit outside `AuthGate` and have no WS snapshot — `/condor`
  will poll REST like `/patterns` and `/opening` do.

---

## 6. Proposed Iron Condor architecture

House principles applied: deterministic and auditable (no LLM in the trade path),
floors-first scoring, shadow-first deployment, every threshold configurable and
pre-registered, NO TRADE as the default output, log-before-optimize.

**New backend package `app/condor/`** (isolated; zero changes to the directional
engine's production path):

| Module | Responsibility |
|---|---|
| `snapshots.py` | **Ships first.** Persist per-strike chain snapshots (LTP, bid/ask, OI, volume, IV) for all subscribed strikes every N min to SQLite `.condor_chain.db` + daily IV summary. This is the dataset every later phase depends on. |
| `greeks.py` | Closed-form BS delta/gamma/theta/vega + on-demand IV for any subscribed strike (reuses `iv.bs_price`/`implied_vol`; not limited to ATM±5). |
| `chain_view.py` | Condor's read of the tick cache: per-leg quote with bid/ask, spread %, exchange-timestamp age; **fails closed** on missing data. Data-quality report per evaluation. |
| `regime.py` | Range classifier (separate from `signals/regime.py`): directional-vote balance persistence over N bars, ADX, BB-width percentile, ATR stability, day-range vs typical, VWAP round-trips, VIX/ATR-spike vetoes → label + confidence + measured range levels (from `patterns/levels.py` + OI walls). |
| `expected_move.py` | ATM straddle · IV·σ√T · ATR-based · realized vol (new HV fn). All four displayed; disagreement is itself a signal. |
| `engine.py` | Deterministic pipeline: data-quality gate → regime gate → volatility filter (VIX percentile, IV level, IV-vs-RV) → strike selection (delta targets by profile ∩ expected move ∩ OI walls ∩ S/R, liquidity floors) → wing selection (width vs credit/max-loss/margin) → economics (credit band, breakevens, POP, net Greeks, margin estimate) → breakout-risk score → entry-timing filter → 0–100 score with displayed components → card or NO TRADE with reasons. |
| `models.py` | `CondorLeg{side, right, strike, token, quote, iv, delta…}`, `CondorCard{legs[4], credit band, economics, scores, reasons[], risks[]}`, `CondorPosition`. |
| `store.py` + archive | Store/reconcile per house pattern; JSONL archive; decision trace. |
| `monitor.py` | Position monitoring (sign-aware, combined premium): P&L, distance-to-shorts, net delta, trade-health score, profit-target/stop/adjustment/exit statuses; CAS-aware close window; deterministic adjustment evaluation priced from the live chain (suggest only when the repriced structure improves). |
| `api/routes_condor.py` | `GET /condor/{symbol}`, history, config, what-if (server-side BS scenario grid: spot × days × IV shift). |

**Loop:** one feed-lifetime `_condor_loop` at 30–60 s (condor conditions move slowly;
no need for the 5 s cadence), evaluating on closed bars like the incumbent engine.

**Frontend:** `/condor` module tab per the established 3-file pattern
(`app/condor/page.tsx`, `components/CondorLab.tsx`, `ModuleSwitcher` entry +
`activeKey` ladder), REST polling, payoff diagram as an SVG path in the `Spark` idiom,
`OptionChainTable` embedded with leg highlighting, `api.ts` types + methods.

**Deployment sequence (maps to the request's phases):**
- Phase 3a: snapshots + greeks + expected move + chain view (pure data, no signals).
- Phase 3b: regime + engine + tab, **advisory/shadow only** — cards displayed and
  archived, not pushed as trade alerts until the ledger has a sample.
- Phase 3c: position monitoring for manually-entered condors (journal entry by hand;
  multi-leg Kite basket is a separate decision — see Q1).
- Phase 5: two-tier validation — (a) range-call validation on the 3-y spine
  (underlying-graded, honest about premium-blindness, like the existing directional
  backtest); (b) premium-true evaluation accumulating from day-one snapshot logging.

---

## 7. Exact files/modules recommended for modification

**New (backend):** `app/condor/` package as above; `backend/tests/test_condor_*.py`.

**Modified (backend, all small and additive):**
- `app/services.py` — construct condor service, add `_condor_loop`, wire snapshot
  logger; clear on `_stop_locked` (note existing paper-service omission there).
- `app/main.py` — register `routes_condor` router; archive merge on boot if needed.
- `app/config.py` — condor settings block (profiles, delta bands, wing widths, score
  threshold, credit floors, entry window, snapshot cadence, max breakout risk…).
- `app/options/iv.py` — none required (Greeks live in `condor/greeks.py`); optionally
  export a shared `RISK_FREE` to kill the `assistant.py` duplicate.
- `backend/.events.json` — recreate from the example (operational fix, not code).

**Modified (frontend):**
- `components/ModuleSwitcher.tsx` — MODULES entry + activeKey ladder (both).
- `app/condor/page.tsx`, `components/CondorLab.tsx` (+ subcomponents: CondorCard,
  PayoffDiagram, RangePanel, MonitorPanel, HistoryPanel) — new.
- `lib/api.ts` — condor types + methods.
- `lib/tradeMath.ts` — **do not overload**; new `lib/condorMath.ts` with sign-aware
  net-credit economics + 4-leg charges, mirrored by a backend twin and pinned by the
  same worked-example test pattern as `charges.py`/`tradeMath.test.ts`.

**Deliberately untouched:** `signals/engine.py`, `signals/regime.py`,
`signals/scoring.py`, `options/chain.py`, `trades/*` (the directional engine keeps
running exactly as-is; the condor module reads shared state, writes its own stores).

---

## 8. Questions / assumptions that materially affect implementation

1. **Execution boundary.** The codebase currently refuses to ever construct a SELL
   order (except the guarded protect stop). A condor is 2 short legs. Assumption:
   the module is **advisory-only display + manual execution in Kite** (matching the
   app's philosophy), with position monitoring on manually-journaled entries. Extending
   the Kite basket hand-off to a 4-leg basket (with SELLs, correct ordering: buy hedges
   first) is possible but is a philosophy change that needs an explicit yes.
2. **Instruments.** Engine currently signals NIFTY only. Assumption: condor v1 =
   **NIFTY weekly**; BANKNIFTY (monthly expiries only since the weekly discontinuation)
   as a config-gated follow-up.
3. **Margin source.** Kite's `basket_order_margins` (accurate, includes hedge benefit,
   adds a REST dependency + auth-time coupling) vs a conservative
   `width − credit` estimate (offline, overstates margin ~2×). Recommend: estimate in
   v1, API upgrade later. Needs a call.
4. **Backtest honesty.** Accept a clearly-labelled synthetic-premium backtest
   (BS-reconstructed from historical underlying + assumed IV), or hold condor
   expectancy claims until logged snapshots accumulate (house rule)? Recommend the
   latter, with the range-call backtest shipping in the meantime.
5. **Expiry scope.** Nearest weekly only (0–7 DTE), or add the next-weekly universe
   now? 0–1 DTE condors are a different (gamma) game — assumption: default profile
   targets 2–7 DTE and warns below 2, consistent with the Phase-12 finding that
   2–4 DTE was the only positive expiry bucket for the directional engine.
6. **Snapshot budget.** 5-min snapshots × ~244 subscribed option tokens ≈ 18k rows/day
   ≈ a few MB/day in SQLite (backup-included). Acceptable? Retention policy?
7. **Config surface.** Per house convention, thresholds live in `.env` (not
   mid-session editable) with only deliberate items in the runtime overlay. Assumption:
   same for condor; the "Configuration Panel" is read-mostly with a handful of
   overlay fields (profile, score threshold, profit-target %).
8. **CAS.** The 15:15–15:35 NIFTY freeze changes expiry-day exit mechanics (last
   liquid exit ~15:10–15:15). Assumption: the monitor treats 15:10 as the effective
   expiry-day exit deadline, mirroring `_INTRADAY_EXIT_MIN`.

---

*Generated 11-Aug-2026 from six parallel subsystem audits (core architecture, options,
market data, signal engine, frontend, trades/backtest/news/patterns) with file:line
verification, plus the 30-Jul and 10-Aug audit documents. No production code changed.*
