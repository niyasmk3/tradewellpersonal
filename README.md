# Tradewell — AI F&O Intraday Signal Engine

A **real-time F&O advisory and decision-support** tool for Indian index options
(NIFTY / BANKNIFTY / FINNIFTY). It analyses the live market and (from Phase 2)
surfaces high-quality, selective CE/PE signals with entry, stop-loss, targets,
confidence and reasons.

> **Advisory only.** Tradewell never places, modifies, or exits orders. It uses
> the Kite login purely for market data; you execute every trade manually in
> Kite. It is intended for personal decision-support, not automated trading and
> not investment advice.

## Status — Phase 5 complete (Backtesting)

| Phase | Scope                                             | State        |
| ----- | ------------------------------------------------- | ------------ |
| **1** | Kite auth, live ticks, candles, indicators, chain | ✅ done      |
| **2** | Rule-based signal engine (regime → score → risk)  | ✅ done      |
| **3** | Active signal monitoring + manual trade journal   | ✅ done      |
| **4** | News intelligence (Claude sentiment)              | ✅ done      |
| **5** | Directional backtesting on Kite historical data   | ✅ done      |
| 6     | ML probability models                             | planned      |

The Phase-2 engine runs two **trading modes** you can toggle in the dashboard:
**Intraday** (3m candles, weekly options, ATM/OTM strikes, tight stop, 8-min
validity) and **Positional / Swing** (15m candles, monthly options, ATM/ITM
strikes, wider stop, bigger targets, multi-session validity). Positional is
framework-complete but shallow until the Kite Historical Data add-on provides
daily/higher-timeframe context.

## Quick start

You need an **active Kite Connect app** (API key + secret). Two terminals:

```bash
# 1) Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add KITE_API_KEY + KITE_API_SECRET
# --host 127.0.0.1 matters: the API has no auth (positions/P&L are readable),
# so keep it loopback-only unless you add auth in front of it.
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 2) Frontend
cd frontend
npm install
npm run dev                   # http://localhost:3000
```

Open http://localhost:3000, complete the one-time daily Kite login on the
landing screen, and the dashboard goes live.

## What Phase 1 gives you

- **Market status bar** — per-index LTP / change, India VIX, market open/closed,
  live WS connection indicator.
- **Candlestick chart** (1/3/5/15m) with VWAP + EMA 9/20 overlays.
- **Indicator panel** — VWAP, EMA 9/20/50, RSI, ATR, ADX, Supertrend, computed
  from the near-month future.
- **Live option chain** — CE/PE OI, OI change, volume, LTP, ATM highlight, PCR.

## Phase 5 — Backtesting

The **Backtest** panel (NIFTY, in the dashboard) replays the *same* regime
classifier and scoring engine over historical near-month **futures** candles
fetched from Kite (needs the **Historical Data add-on**), and grades every
signal on the underlying move. Pick a mode (Intraday 3m / Positional 15m) and a
window (10–60 days), then run.

It reports win-rate, **expectancy (R per trade)**, profit factor, average
win/loss R, max drawdown, total R, a CE/PE split, an equity curve and the full
trade list (entry/exit/R/outcome).

**It is deliberately a *directional* backtest** — read the metrics with these
limits in mind:

- Grades the **signal thesis** (did the underlying reach the target-implied
  level or the invalidation level first?), **not exact option P&L** — premium
  fills, theta decay and IV are not modelled.
- The live-only inputs — **option-chain OI and news sentiment** — are absent
  from history, so scoring uses the **technical components only** (price action,
  trend, volume, volatility), rescaled to 0–100. Selection therefore differs
  from the live engine, which also weights OI + news.
- Entry is the **next bar's open** (reaction lag); slippage is applied against
  you on entry and exit; on a bar that spans both stop and target, the **stop is
  assumed first** (conservative). Intraday never holds overnight; positional
  holds up to a bar cap.

Treat the output as a directional read on the engine's edge, not a P&L promise.

## Stack

- **Backend:** Python · FastAPI · Kite Connect · pandas/numpy · WebSocket
- **Frontend:** Next.js (App Router) · TypeScript · Tailwind · Lightweight Charts
- **Phase 2 datastores:** TimescaleDB + Redis (`docker-compose.yml`)

See [`backend/README.md`](backend/README.md) and
[`frontend/README.md`](frontend/README.md) for details.

## Design decisions worth knowing

1. **Indicators run on the near-month future, not spot** — the index spot has no
   traded volume, so VWAP / volume-based analysis needs the future. Spot LTP is
   still displayed for level references.
2. **Candles are built from ticks** — Kite streams ticks, not candles; the
   backend aggregates OHLCV itself, aligned to the IST session grid.
3. **Option OI comes from FULL-mode ticks**, not a separate chain API (Kite has
   none); "OI change" is the build since the feed started. IV is Phase 2.
4. **Selective by design** — Phase 2's engine is meant to produce 1–4 quality
   setups on a typical day, gated on market regime and a 0–100 score, not a
   constant stream of signals.
5. **Self-healing feed (post-audit hardening)** — daily re-login restarts the
   ticker with the fresh token and re-resolves the day's expiries; a supervisor
   auto-recovers a dead ticker and rolls the universe over at IST midnight; the
   status bar shows **"feed stale"** whenever the market is open but ticks stop.
   Candles are seeded from the Kite historical API at feed start, so a restart
   mid-session loses nothing and indicators are warm immediately. Signals are
   evaluated on **closed candles only**. Instruments dumps and news dedupe are
   disk-cached per day; news polls pause outside 07:00–17:00 IST weekdays. The
   dashboard can chime + desktop-notify on each new valid signal (🔔 toggle).
6. **News + mood sources are RSS-verified** — the news pipeline ingests RSS only,
   so feeds are chosen by what actually parses live: ET Markets + ET Economy,
   Livemint Markets, Hindu BusinessLine Markets (3 publishers). Moneycontrol
   (stale) and Business Standard (dead) RSS were dropped. Per-stock platforms
   (Trendlyne, Tickertape) have no RSS/API and aren't used. The **Market Mood
   Index** (Tickertape Fear/Greed gauge) is shown as read-only context via
   `GET /market/mood` — it does **not** feed the signal score.
