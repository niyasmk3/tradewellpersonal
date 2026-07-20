# Tradewell Frontend (Phase 1)

Next.js (App Router) + TypeScript + Tailwind dashboard for the Tradewell backend.

## Run

```bash
npm install
cp .env.local.example .env.local   # only if backend isn't on localhost:8000
npm run dev                        # http://localhost:3000
```

The backend must be running on `http://localhost:8000` (override via
`NEXT_PUBLIC_API_BASE` / `NEXT_PUBLIC_WS_URL`).

## Structure

```
app/
├── layout.tsx           root layout + globals
└── page.tsx             AuthGate → Dashboard
components/
├── AuthGate.tsx         daily Kite login flow
├── Dashboard.tsx        layout + data wiring (WS + polling)
├── MarketStatusBar.tsx  index chips, VIX, market status
├── PriceChart.tsx       Lightweight Charts candles + VWAP/EMA overlays
├── IndicatorPanel.tsx   indicator tiles
└── OptionChainTable.tsx live CE/PE chain + PCR
lib/
├── api.ts               REST client + types (mirror backend schemas)
├── useLiveData.ts       WebSocket hook (auto-reconnect)
├── usePolling.ts        interval fetch hook
└── format.ts            number / time formatters
```

Live index prices arrive over the WebSocket (1s). Candles, indicators, and the
option chain are polled over REST (2–3s) keyed on the selected symbol/timeframe.
VWAP + EMA chart overlays are computed client-side from the candle series.
