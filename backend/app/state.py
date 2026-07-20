"""Process-wide live market state.

A single ``MarketState`` instance is shared between the KiteTicker thread
(writer) and the FastAPI asyncio loop (reader). Candle engines carry their own
locks; a coarse state lock guards the tick / option dictionaries.

Everything here is in-memory. Phase 2 swaps the option-chain and candle stores
for Redis + TimescaleDB behind these same accessors.
"""
from __future__ import annotations

import threading
import time

from app.market.candles import CandleEngine
from app.models.schemas import (
    OptionChain,
    UnderlyingSnapshot,
    VixSnapshot,
)


def _vix_status(ltp: float | None) -> str | None:
    if ltp is None:
        return None
    if ltp < 12:
        return "Calm"
    if ltp < 16:
        return "Stable"
    if ltp < 20:
        return "Elevated"
    return "High"


class UnderlyingMeta:
    """Static wiring for one tracked index: spot + near-month future tokens."""

    def __init__(
        self,
        symbol: str,
        spot_token: int,
        spot_tradingsymbol: str,
        strike_step: int,
        fut_token: int | None = None,
        fut_tradingsymbol: str | None = None,
        lot_size: int = 1,
    ) -> None:
        self.symbol = symbol
        self.spot_token = spot_token
        self.spot_tradingsymbol = spot_tradingsymbol
        self.strike_step = strike_step
        self.fut_token = fut_token
        self.fut_tradingsymbol = fut_tradingsymbol
        self.lot_size = lot_size


class MarketState:
    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Wiring / static
        self.underlyings: dict[str, UnderlyingMeta] = {}
        self.candle_engines: dict[int, CandleEngine] = {}  # token -> engine (fut tokens)

        # Live values
        self.ticks: dict[int, dict] = {}                   # token -> latest tick fields
        self.option_chains: dict[str, OptionChain] = {}    # "SYMBOL:expiry" -> chain
        self.oi_prev: dict[int, float] = {}                # option token -> last OI (for delta)
        self.vix_token: int | None = None

        # Status
        self.ticker_connected: bool = False
        self.ticker_dead: bool = False       # reconnect budget exhausted — needs a feed restart
        self.instruments_loaded: bool = False
        self.user_id: str | None = None

    # ---- wiring ----------------------------------------------------------
    def register_underlying(self, meta: UnderlyingMeta) -> None:
        with self._lock:
            self.underlyings[meta.symbol] = meta
            if meta.fut_token:
                self.candle_engines.setdefault(meta.fut_token, CandleEngine(meta.fut_token))

    def engine(self, token: int) -> CandleEngine | None:
        return self.candle_engines.get(token)

    def engine_for_symbol(self, symbol: str) -> CandleEngine | None:
        meta = self.underlyings.get(symbol.upper())
        if meta and meta.fut_token:
            return self.candle_engines.get(meta.fut_token)
        return None

    # ---- tick ingestion (ticker thread) ---------------------------------
    def on_tick(self, tick: dict) -> None:
        token = tick.get("instrument_token")
        if token is None:
            return
        with self._lock:
            self.ticks[token] = tick

        engine = self.candle_engines.get(token)
        if engine is not None:
            ts = tick.get("ts") or int(time.time())
            engine.add_tick(tick.get("last_price"), tick.get("volume_traded"), ts)

    # ---- reads (API thread) ---------------------------------------------
    def _snapshot_for(self, meta: UnderlyingMeta) -> UnderlyingSnapshot:
        spot = self.ticks.get(meta.spot_token, {})
        fut = self.ticks.get(meta.fut_token, {}) if meta.fut_token else {}
        ohlc = spot.get("ohlc") or {}
        ltp = spot.get("last_price")
        prev_close = ohlc.get("close")
        change = change_pct = None
        if ltp is not None and prev_close:
            change = round(ltp - prev_close, 2)
            change_pct = round((ltp - prev_close) / prev_close * 100, 2)
        return UnderlyingSnapshot(
            symbol=meta.symbol,
            tradingsymbol=meta.spot_tradingsymbol,
            instrument_token=meta.spot_token,
            ltp=ltp,
            change=change,
            change_pct=change_pct,
            day_open=ohlc.get("open"),
            day_high=ohlc.get("high"),
            day_low=ohlc.get("low"),
            prev_close=prev_close,
            updated_at=spot.get("ts"),
            fut_token=meta.fut_token,
            fut_ltp=fut.get("last_price"),
            fut_tradingsymbol=meta.fut_tradingsymbol,
        )

    def underlying_snapshots(self) -> list[UnderlyingSnapshot]:
        with self._lock:
            metas = list(self.underlyings.values())
            return [self._snapshot_for(m) for m in metas]

    def underlying_snapshot(self, symbol: str) -> UnderlyingSnapshot | None:
        with self._lock:
            meta = self.underlyings.get(symbol.upper())
            return self._snapshot_for(meta) if meta else None

    def vix_snapshot(self) -> VixSnapshot | None:
        if self.vix_token is None:
            return None
        with self._lock:
            tick = self.ticks.get(self.vix_token, {})
        ltp = tick.get("last_price")
        ohlc = tick.get("ohlc") or {}
        prev = ohlc.get("close")
        change_pct = round((ltp - prev) / prev * 100, 2) if ltp and prev else None
        return VixSnapshot(ltp=ltp, change_pct=change_pct, status=_vix_status(ltp))

    def last_tick_age(self) -> int | None:
        """Seconds since the newest tick on any tracked underlying (spot or
        future). None until the first tick lands. The single catch-all staleness
        signal: every feed failure mode ends up here as a growing age."""
        with self._lock:
            tokens: list[int] = []
            for m in self.underlyings.values():
                tokens.append(m.spot_token)
                if m.fut_token:
                    tokens.append(m.fut_token)
            newest = max(
                (self.ticks[t].get("ts") or 0 for t in tokens if t in self.ticks),
                default=0,
            )
        return int(time.time()) - newest if newest else None

    def set_option_chain(self, key: str, chain: OptionChain) -> None:
        # `key` is a compound "SYMBOL:expiry" key (see kite.instruments.chain_key).
        with self._lock:
            self.option_chains[key] = chain

    def get_option_chain(self, key: str) -> OptionChain | None:
        with self._lock:
            return self.option_chains.get(key)


# Module-level singleton.
market_state = MarketState()
