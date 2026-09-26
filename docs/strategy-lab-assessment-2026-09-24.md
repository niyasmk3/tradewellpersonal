# Strategy Lab — Technical Assessment (24-Sep-2026)

**Request:** a new main-nav tab ("Algo Strategy Lab") holding a 40-strategy NIFTY-only
research + automated paper-trading platform: catalog, event-driven backtester, honest
costs, daily paper scheduler, champion/challenger learning, regime analysis, shared risk
engine, daily reports. No live orders. No fabricated results.

**Verdict up front:** the codebase already contains most of the hard infrastructure
(data feed, indicators, regime classifier, Indian F&O cost model, IV solver, NSE
calendar, in-process scheduler pattern, execution-adapter framework). The binding
constraint is **historical options data** — Kite deletes expired option contracts, so
genuine option-premium history exists only inside the condor `chain_snap` archive
(12-Aug-2026 → today, ~921k real quote rows: LTP/bid/ask/OI/volume/IV per strike).
Every option-strategy backtest is therefore honestly bounded to that window (growing
one day per session), and everything earlier is **data-blocked**, not modelled around.

---

## 1. Existing stack (inspected 24-Sep)

| Layer | What exists |
|---|---|
| Backend | FastAPI + uvicorn, Python **3.9.6** (system), pydantic v2, pandas/numpy, kiteconnect. 16 routers registered flat in `main.py`; per-module packages under `backend/app/`. |
| Scheduler | No celery/APScheduler — plain `while True: await asyncio.sleep()` loops started in `lifespan` (`ops_loop`, `gold_live_loop`, `algo_loop`, …). Job idempotency via IST-date stamps in dot-JSON state files; pure gate functions for testability (`ops.py` pattern). |
| Persistence | No DB server, by decision. Dot-files in `backend/`: JSON (small mutable), JSONL (append-only ledgers), **stdlib sqlite** for bulk time series (`.patterns_candles.db`, `.condor_chain.db`, `.gold_*.db`, `.closing_vix.db`). Atomic tmp+`replace()` writes. Nightly tar backup (`ops.py`) with sqlite online-backup API. |
| Market data | Kite: instrument master cached daily (per-contract lot sizes), FULL-mode WS ticks → in-memory `CandleEngine` (1/3/5/15m), historical API chunked ≤60d at ~3 req/s, VIX daily. Option chain built from ticks (OI, depth); IV via own Black-Scholes bisection (`options/iv.py`). |
| Calendar/TZ | `market/calendar.py` is the single source of truth (2026 holiday set, IST helpers). **IST-only discipline is a hard project rule** (guarded by `tests/test_ist_only.py`). |
| Costs | `paper/charges.py` + `condor/charges.py`: brokerage ₹20/order, STT sell 0.15% (w.e.f. 01-Apr-2026), exchange txn 0.03503%, IPFT, SEBI, GST 18%, stamp 0.003% buy. Options only — futures rates missing (will be added). |
| Regime | `signals/regime.py::classify` — 7-vote directional model + compression/news-vol/ADX special cases, 11 labels. |
| Execution | `app/algo`: frozen contract, 28-check fail-closed guard, dry/paper/live broker rungs (live deliberately unbuildable), append-only ledger, kill switch. Rollout ladder — nothing above A1 authorised. The Lab does **not** touch this; a future adapter could. |
| Backtesting | `app/backtest`: directional replay of the live signal engine on near-month futures, R-multiples, no options/costs. Good precedent (entry-delay, stop-before-target, day-context) but single-purpose — not a general engine. |
| Paper | `app/paper`: forward-simulates the live signal engine's cards on live ticks with honest-fill rules and A/B shadow ledgers. Also single-purpose. |
| Frontend | Next 14 app router, Tailwind (dark-only design tokens: `panel/panel2/edge/bull/bear/accent/muted`), `usePolling`, flat `api` object in `lib/api.ts`, `lightweight-charts` v5, per-module Lab components. Tab = page + Lab component + `ModuleSwitcher` entry. |

## 2. Data inventory (real, on disk today)

| Dataset | Span | Size | Use |
|---|---|---|---|
| NIFTY index 5-min (`.patterns_candles.db`) | ~3 years → 23-Sep | 57,726 bars | Signal spine for index-driven strategies; regime history. Volume = NIFTYBEES proxy. |
| Option chain snapshots (`.condor_chain.db` `chain_snap`) | **12-Aug-2026 → today** | 920,902 rows | THE real options history: per-strike LTP/bid/ask/OI/volume/IV, nearest+monthly expiries, ~ATM±30. Grows daily. Cannot be rebuilt — the only source of truth for option fills. |
| India VIX daily (`.closing_vix.db` + feed seed) | ~1y+ | — | VIX regime features. |
| NIFTY futures candles | **not yet stored** | — | Kite historical serves the current contract; `continuous=1` may serve expired months — **unverifiable right now (Kite token currently invalid)**. Futures sync (incl. OI via `oi=1`) is built; span confirmed at first authenticated sync. |
| Historical option contracts (pre 12-Aug) | **does not exist** | — | Kite deletes expired option instruments. Hard blocker; disclosed per strategy. |

## 3. Consequences for the 40 strategies

- **Category A (futures, F01–F10):** backtestable on real futures candles once synced
  (span pending the `continuous=1` check; worst case ~last 60–90 d of the current +
  recent contracts, plus 3y of index-spine *signal research* clearly labelled
  index-proxy). F10 needs futures OI → Kite `oi=1` historical.
- **Categories B/C/D/E (options, 22 strategies):** genuine backtests **only inside the
  chain-snap window** (~30 trading days today). Small-n warnings mandatory. Before
  12-Aug: "Insufficient historical data", never an index-proxy or Black-Scholes
  substitute (per spec: no invented premiums).
- **Category F:** Q01 (regime) runs on 3y index; Q02 (ML) implemented as a transparent
  numpy logistic model (no sklearn dep on Py 3.9) with walk-forward only; Q03 ensembles
  other strategies' signals. Option execution again window-bounded.

## 4. Design decisions

1. **New package `backend/app/lab/`**, routes at `/lab`, frontend `/lab` +
   `StrategyLab.tsx`, ModuleSwitcher label **"Strategy Lab"** — "Algo" is already taken
   by the execution module; two tabs named Algo would be a footgun.
2. **Storage: one sqlite DB `.lab.db`** (WAL) — versions, runs, trades, paper ledger,
   experiments, risk/data events, reports. Immutable result rows (append-only, never
   overwrite on re-run); added to the nightly ops backup list (`.lab_futures.db` is
   refetchable and deliberately not backed up).
3. **Event-driven engine, one code path**: closed-bar signals → next-bar-open fills
   (futures) / first fresh chain quote ≥ entry-latency after signal (options, buy at
   ask+slip, sell at bid−slip); stop-before-target intrabar (configurable); multi-leg
   legs filled individually, order rejected if any leg lacks a fresh quote; strikes
   only from chain rows that actually existed at that timestamp.
4. **Costs date-aware**, extending the audited constants in `paper/charges.py`;
   futures rates added (STT 0.02% sell, exch 0.00173%) with sources in comments.
5. **Margin = documented estimate** (no SPAN feed): short options ≈ max(15% notional −
   OTM, 7.5% notional) + premium; futures ≈ 12% notional. Config constants, labelled
   estimates everywhere they surface.
6. **Paper engine** = `lab_loop` in lifespan (gold_live_loop pattern): pre-market
   checks, in-session closed-bar evaluation off the live `CandleEngine` + tick/chain
   fills, post-close reconcile + immutable daily ledger + learning report. Restart-safe
   (all state in `.lab.db`). Flags: `LAB_ENABLED` (default true, advisory),
   `LAB_PAPER_ENABLED` (default **false**, committed; enabled per-machine in `.env` —
   the gold/condor pattern).
7. **Learning**: champion/challenger with bounded param perturbations from each
   strategy's `ParamSpec` ranges, DEV/TEST walk-forward, configurable evidence gates
   (min trades/days), experimental paper arm, promotion only within paper, every
   decision a row in `experiment`. Live promotion does not exist in code.
8. **Risk engine** checked centrally in the paper order path: portfolio caps, daily
   loss, drawdown, expiry-day and short-premium exposure caps, emergency stop
   (persisted latch; blocks new paper orders, existing positions per configured
   policy).
9. **Existing modules untouched** except additive registrations (`main.py`,
   `config.py`, `api.ts`, `ModuleSwitcher.tsx`). The user's uncommitted in-flight work
   (closing realquote, GoldChart) is not touched.

## 5. Blockers / limitations disclosed

- **Options history starts 12-Aug-2026.** All option-strategy metrics carry low-n
  warnings until months of window accumulate. This is a data fact, not a code gap.
- **Kite token is dead right now** → futures/index syncs and the `continuous=1` span
  check run at the next login; readiness panels show "awaiting sync" until then.
- Chain snapshots are ~90 s cadence → intrabar option stops/targets resolve at
  snapshot granularity; conservative resolution documented in the engine.
- Greeks history = BS-derived from stored IV (r=6.5%), labelled derived.
- No live trading anywhere in the Lab; the only path to execution remains the separate
  `app/algo` ladder with its own authorisation gates.
