# Tradewell Backend (Phase 1)

FastAPI service that authenticates to Kite, streams live ticks, builds candles +
indicators, assembles the option chain, and serves it over REST + WebSocket.

## Run

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env         # then fill in KITE_API_KEY / KITE_API_SECRET
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs for the interactive API.

## Daily login

Kite access tokens expire every morning (~07:30 IST). Each trading day:

1. `GET /auth/status` → returns a `login_url` while unauthenticated.
2. Open it, log into Kite. Kite redirects to your app's redirect URL with
   `?request_token=…`. Copy that value.
3. `POST /auth/session` `{"request_token": "…"}` → exchanges it for an access
   token and starts the live feed.

The frontend's login screen does all three for you. You can also paste a fresh
`KITE_ACCESS_TOKEN` into `.env` to skip the browser step on restart.

## Endpoints

| Method | Path                          | Purpose                              |
| ------ | ----------------------------- | ------------------------------------ |
| GET    | `/health`                     | liveness + auth/feed flags           |
| GET    | `/auth/status`                | auth state + login URL               |
| POST   | `/auth/session`               | exchange request_token, start feed   |
| GET    | `/market/snapshot`            | underlyings + VIX + market status    |
| GET    | `/market/{symbol}/candles`    | OHLCV candles (`?tf=1m|3m|5m|15m`)   |
| GET    | `/market/{symbol}/indicators` | latest indicator snapshot            |
| GET    | `/options/{symbol}`           | live option chain + PCR              |
| WS     | `/ws`                         | pushes `MarketSnapshot` every second |

`{symbol}` ∈ `NIFTY`, `BANKNIFTY`, `FINNIFTY` (per `TRACK_UNDERLYINGS`).

## Architecture notes

- **Ticks, not candles.** KiteTicker streams ticks on its own thread; the
  `CandleEngine` aggregates them into 1/3/5/15m OHLCV. A `MarketState` singleton
  is the hand-off point between that thread and the asyncio API.
- **Indicators use the near-month FUTURE.** The index spot has no volume, so
  VWAP / volume are sourced from the future (which tracks the index closely).
  Spot LTP is still shown as the reference price for levels.
- **Option chain from FULL-mode ticks.** OI + volume arrive in the tick stream;
  `OptionChainBuilder` reads them each poll and diffs OI vs a session baseline.
  IV is not provided by Kite and is deferred to Phase 2 (Black-Scholes).
- **In-memory only.** Postgres/TimescaleDB + Redis (see `../docker-compose.yml`)
  come online in Phase 2 for candle persistence and signal state.

## Layout

```
app/
├── main.py            FastAPI app, lifespan, WS endpoint
├── config.py          settings (.env)
├── services.py        feed orchestration (instruments → ticker → option loop)
├── state.py           MarketState singleton (thread ↔ asyncio hand-off)
├── kite/
│   ├── client.py      auth / session wrapper
│   ├── instruments.py instrument sync + token resolution
│   └── ticker.py      KiteTicker manager
├── market/
│   ├── candles.py     tick → OHLCV aggregation
│   └── indicators.py  VWAP, EMA, RSI, ATR, ADX, Supertrend, BB-width
├── options/chain.py   option-chain assembly + OI delta + PCR
├── api/               auth / market / options routes + ws broadcaster
└── models/schemas.py  shared pydantic contracts
```
