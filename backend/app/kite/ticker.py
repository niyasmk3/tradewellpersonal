"""KiteTicker WebSocket manager.

KiteTicker runs its own (Twisted) reactor thread. We start it in threaded mode,
subscribe every resolved token in FULL mode (FULL carries OHLC + open interest),
normalise each tick, and hand it to ``MarketState``. The FastAPI asyncio loop
never touches the socket directly — it only reads the shared state.
"""
from __future__ import annotations

import logging
from datetime import timezone, timedelta

from kiteconnect import KiteTicker

from app.state import MarketState

log = logging.getLogger("tradewell.ticker")

_IST = timezone(timedelta(hours=5, minutes=30))


def _epoch(dt) -> int | None:
    """Kite exchange timestamps are naive IST datetimes -> true UTC epoch."""
    if dt is None:
        return None
    try:
        return int(dt.replace(tzinfo=_IST).timestamp())
    except Exception:
        return None


class TickerManager:
    def __init__(self, state: MarketState) -> None:
        self.state = state
        self._kws: KiteTicker | None = None
        self._tokens: list[int] = []

    def start(self, api_key: str, access_token: str, tokens: list[int]) -> None:
        self._tokens = tokens
        kws = KiteTicker(api_key, access_token)
        kws.on_ticks = self._on_ticks
        kws.on_connect = self._on_connect
        kws.on_close = self._on_close
        kws.on_error = self._on_error
        kws.on_reconnect = self._on_reconnect
        kws.on_noreconnect = self._on_noreconnect
        self._kws = kws
        self.state.ticker_dead = False
        log.info("Connecting KiteTicker for %d tokens", len(tokens))
        kws.connect(threaded=True)

    def stop(self) -> None:
        if self._kws is not None:
            try:
                self._kws.close()
            except Exception:  # pragma: no cover
                pass
        self.state.ticker_connected = False

    # ---- callbacks -------------------------------------------------------
    def _on_connect(self, ws, response) -> None:  # noqa: ANN001
        log.info("KiteTicker connected; subscribing %d tokens (FULL mode)", len(self._tokens))
        ws.subscribe(self._tokens)
        ws.set_mode(ws.MODE_FULL, self._tokens)
        self.state.ticker_connected = True
        self.state.ticker_dead = False

    def _on_ticks(self, ws, ticks) -> None:  # noqa: ANN001
        for t in ticks:
            t["ts"] = _epoch(t.get("exchange_timestamp")) or _epoch(t.get("last_trade_time"))
            self.state.on_tick(t)

    def _on_close(self, ws, code, reason) -> None:  # noqa: ANN001
        log.warning("KiteTicker closed: %s %s", code, reason)
        self.state.ticker_connected = False

    def _on_error(self, ws, code, reason) -> None:  # noqa: ANN001
        log.error("KiteTicker error: %s %s", code, reason)
        # 403 = Kite rejected the access token (daily ~07:30 IST flush). Invalidate
        # it so /auth/status flips to unauthenticated and the UI shows the login flow.
        if code == 403 or "TokenException" in str(reason):
            from app.kite.client import kite_service  # local import avoids a cycle
            kite_service.invalidate()

    def _on_reconnect(self, ws, attempts) -> None:  # noqa: ANN001
        log.info("KiteTicker reconnecting, attempt %d", attempts)

    def _on_noreconnect(self, ws) -> None:  # noqa: ANN001
        # Reconnect budget exhausted — the ticker will NEVER come back on its own.
        # Flag it so the API/UI can demand a feed restart instead of zombie-ing.
        log.error("KiteTicker gave up reconnecting — feed restart required")
        self.state.ticker_connected = False
        self.state.ticker_dead = True
