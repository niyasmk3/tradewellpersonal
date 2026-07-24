"""WebSocket fan-out of the live market snapshot to browser clients.

A single background task broadcasts a compact ``MarketSnapshot`` (underlyings +
VIX + status) every ``BROADCAST_SECONDS``. The heavier chart / option-chain data
is pulled over REST by the frontend on its own cadence.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import WebSocket

from app.config import get_settings
from app.market import calendar as mcal
from app.models.schemas import MarketSnapshot
from app.state import market_state

log = logging.getLogger("tradewell.ws")


def is_market_open() -> bool:
    # Holiday-aware (round-2 audit: weekday holidays previously showed
    # "market open" + a false FEED STALE badge all day).
    return mcal.is_market_open()


_STALE_AFTER_S = 15  # market open but no tick for this long => stale


def build_snapshot() -> MarketSnapshot:
    from app.services import feed

    open_now = is_market_open()
    age = market_state.last_tick_age()
    return MarketSnapshot(
        server_time=int(time.time()),
        market_open=open_now,
        underlyings=market_state.underlying_snapshots(),
        vix=market_state.vix_snapshot(),
        last_tick_age=age,
        feed_stale=bool(open_now and (age is None or age > _STALE_AFTER_S)),
        alerts_armed=bool(getattr(feed, "alerts_armed", False)),
    )


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        log.info("WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        log.info("WS client disconnected (%d total)", len(self._clients))

    async def _send_one(self, ws: WebSocket, payload: dict) -> WebSocket | None:
        """Send with a hard timeout. A client whose TCP has silently stalled
        (laptop lid closed) never raises — without the timeout it would block
        the single broadcaster coroutine and freeze every other dashboard."""
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=2.0)
            return None
        except Exception:
            return ws

    async def broadcast(self, payload: dict) -> None:
        clients = list(self._clients)
        if not clients:
            return
        results = await asyncio.gather(*(self._send_one(ws, payload) for ws in clients))
        for ws in results:
            if ws is not None:
                self.disconnect(ws)


manager = ConnectionManager()


async def broadcaster_loop() -> None:
    interval = get_settings().broadcast_seconds
    while True:
        try:
            if manager._clients:
                await manager.broadcast(build_snapshot().model_dump())
        except Exception as exc:  # pragma: no cover
            log.warning("broadcast error: %s", exc)
        await asyncio.sleep(interval)
