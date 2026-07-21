"""Tradewell backend — Phase 1 live market dashboard API.

Run:  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    routes_auth,
    routes_backtest,
    routes_kite_basket,
    routes_paper,
    routes_market,
    routes_news,
    routes_options,
    routes_signals,
    routes_trades,
)
from app.api.ws import broadcaster_loop, build_snapshot, manager
from app.config import get_settings
from app.kite.client import kite_service
from app.services import feed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tradewell")


@asynccontextmanager
async def lifespan(app: FastAPI):
    broadcaster = asyncio.create_task(broadcaster_loop())
    supervisor = asyncio.create_task(feed.supervisor())

    # If a valid access token was supplied via .env, start the feed immediately.
    if kite_service.is_authenticated:
        try:
            await feed.start()
        except Exception as exc:
            log.warning("Could not auto-start feed (re-login may be required): %s", exc)
    else:
        log.info("No Kite session yet. POST a request_token to /auth/session to begin.")

    yield

    broadcaster.cancel()
    supervisor.cancel()
    await feed.stop()


app = FastAPI(title="Tradewell API", version="0.1.0", lifespan=lifespan)

_settings = get_settings()
# Local-dev origins the browser may serve the dashboard from. "tradewell" works
# once you map it to 127.0.0.1 in /etc/hosts (see README).
_cors_origins = list(dict.fromkeys([
    _settings.frontend_origin,
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://tradewell:3000", "http://tradewell",
]))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_auth.router)
app.include_router(routes_market.router)
app.include_router(routes_options.router)
app.include_router(routes_signals.router)
app.include_router(routes_trades.router)
app.include_router(routes_news.router)
app.include_router(routes_backtest.router)
app.include_router(routes_kite_basket.router)
app.include_router(routes_paper.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    from app.state import market_state

    return {
        "status": "ok",
        "authenticated": kite_service.is_authenticated,
        "feed_running": feed.running,
        "feed_healthy": feed.healthy,
        "ticker_connected": market_state.ticker_connected,
        "last_tick_age_s": market_state.last_tick_age(),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        # Push one snapshot immediately so the UI paints without waiting a tick.
        await ws.send_json(build_snapshot().model_dump())
        while True:
            # We don't expect client messages; this await just detects disconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)
