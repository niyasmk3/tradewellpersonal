"""Feed orchestration: instrument sync -> ticker -> option-chain refresh loop.

Started once the Kite session is live (either from a token in .env at boot, or
after POST /auth/session at runtime).
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.kite.client import kite_service
from app.kite.instruments import resolve_universe
from app.kite.ticker import TickerManager
from app.options.chain import OptionChainBuilder
from app.news.service import NewsService
from app.news.store import news_store
from app.signals.service import SignalService
from app.signals.store import signal_store
from app.state import market_state
from app.trades.service import TradeMonitorService
from app.trades.store import TradeStore, trade_store

log = logging.getLogger("tradewell.services")

_IST = timezone(timedelta(hours=5, minutes=30))
_TF_TO_KITE = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute"}


def _seed_candles(kite, state) -> int:
    """Preload every futures CandleEngine with the latest session's candles from
    the Kite historical API (requires the Historical Data add-on; best-effort).

    Makes a mid-session restart lossless — without this, EMA50/ADX/BB need
    hours of live ticks to become meaningful again. Blocking; call off-loop.
    """
    to_dt = datetime.now(_IST)
    # 7 days spans long holiday runs (e.g. Thu holiday + weekend + Mon holiday)
    # back to the most recent actual session; the per-session filter below keeps
    # only the latest day's candles.
    from_dt = to_dt - timedelta(days=7)
    seeded = 0
    for meta in list(state.underlyings.values()):
        engine = state.engine(meta.fut_token) if meta.fut_token else None
        if engine is None:
            continue
        for tf, interval in _TF_TO_KITE.items():
            candles = None
            for attempt in (1, 2):
                try:
                    candles = kite.historical_data(meta.fut_token, from_dt, to_dt, interval)
                    break
                except Exception as exc:
                    msg = f"{type(exc).__name__}: {exc}"
                    # No Historical add-on / revoked token: every further call
                    # will fail identically — abandon seeding entirely.
                    if "permission" in msg.lower() or "TokenException" in msg:
                        log.warning("candle seeding unavailable (%s) — continuing without", msg)
                        return seeded
                    # Transient (timeout/network): retry once, then just skip
                    # THIS timeframe — don't abandon the rest (audit finding).
                    log.warning("seed fetch %s/%s attempt %d failed: %s",
                                meta.symbol, tf, attempt, msg)
                    _time.sleep(0.5)
            if candles is None:
                continue
            rows = [
                {"ts": int(c["date"].timestamp()), "open": c["open"], "high": c["high"],
                 "low": c["low"], "close": c["close"], "volume": c.get("volume", 0) or 0}
                for c in candles
            ]
            if rows:
                last_day = (rows[-1]["ts"] + 19800) // 86400
                rows = [r for r in rows if (r["ts"] + 19800) // 86400 == last_day]
                engine.seed(tf, rows)
                seeded += len(rows)
            _time.sleep(0.35)               # stay under Kite's ~3 req/s historical limit
    return seeded


class FeedController:
    def __init__(self) -> None:
        self.ticker: TickerManager | None = None
        self.chain_builder: OptionChainBuilder | None = None
        self.signal_service: SignalService | None = None
        self.trade_monitor: TradeMonitorService | None = None
        self.paper: "PaperTradingService | None" = None
        self.news_service: NewsService | None = None
        self.running: bool = False
        self._tasks: list[asyncio.Task] = []
        # One lock serialises start/stop/restart. Without it, two concurrent
        # POST /auth/session calls both pass the `running` check during the
        # multi-second instruments download and double-start every loop.
        self._lifecycle = asyncio.Lock()
        # Token the current ticker was built with — a re-login with a NEW token
        # must rebuild the ticker (KiteTicker bakes the token in at construction).
        self._token_in_use: str | None = None
        # IST day the feed started on — a running feed that crosses midnight has
        # stale expiries/ATM windows and must be re-resolved for the new day.
        self._started_day: str | None = None

    async def start(self) -> None:
        async with self._lifecycle:
            if self.running:
                return
            await self._start_locked()

    async def restart(self) -> None:
        """Full stop + start. Used after a re-login: picks up the fresh access
        token AND re-resolves the day's expiries / ATM strike window."""
        async with self._lifecycle:
            await self._stop_locked()
            await self._start_locked()

    async def ensure_current_token(self) -> None:
        """Called after /auth/session. If the feed is running on an older token
        (yesterday's, pre-relogin), restart it; if not running, start it."""
        async with self._lifecycle:
            if self.running and self._token_in_use == kite_service.access_token:
                return
            if self.running:
                log.info("New Kite token issued — restarting feed")
                await self._stop_locked()
            await self._start_locked()

    async def _start_locked(self) -> None:
        if not kite_service.is_authenticated:
            raise RuntimeError("Kite session not established")

        settings = get_settings()
        try:
            # instruments() is a blocking network call -> run off the event loop.
            tokens, universes = await asyncio.to_thread(
                resolve_universe, kite_service.kite, settings, market_state
            )
        except Exception as exc:
            # A rejected token must surface as "re-login needed", not a zombie.
            if "TokenException" in type(exc).__name__ or "api_key or access_token" in str(exc):
                kite_service.invalidate()
                raise RuntimeError("Kite token expired — complete the daily login again") from exc
            raise
        self.chain_builder = OptionChainBuilder(market_state, universes, settings.option_chain_depth)

        # Recover the session's candles BEFORE ticks start flowing (no lock
        # contention, and indicators are warm from the first evaluation).
        try:
            n = await asyncio.to_thread(_seed_candles, kite_service.kite, market_state)
            if n:
                log.info("Seeded %d historical candles across futures engines", n)
        except Exception as exc:  # pragma: no cover
            log.warning("Candle seeding failed (continuing live-only): %s", exc)

        # Open journal trades must keep receiving premiums even if their strike
        # has drifted outside the re-centred ATM window (e.g. after a gap).
        open_tokens = [t.token for t in trade_store.all()
                       if t.token and t.status.value in ("entered", "partial")]
        tokens = list(dict.fromkeys([*tokens, *open_tokens]))

        self.ticker = TickerManager(market_state)
        self.ticker.start(settings.kite_api_key, kite_service.access_token, tokens)
        self._token_in_use = kite_service.access_token
        market_state.user_id = kite_service.user_id

        self.signal_service = SignalService(settings, market_state, signal_store)
        self.trade_monitor = TradeMonitorService(market_state, trade_store)
        if settings.paper_trading:
            # Its OWN store file. A paper position must never appear in the
            # real journal, the realised P&L, or the circuit breakers — and
            # must never be reconciled against the broker's position book,
            # where it has no counterpart.
            from pathlib import Path as _Path

            from app.paper.service import PaperTradingService
            paper_store = TradeStore(path=_Path(__file__).resolve().parents[1] / ".paper_trades.json")
            self.paper_store = paper_store
            self.paper = PaperTradingService(settings, market_state, paper_store)
            log.warning("PAPER TRADING ON — simulated fills only, no orders are placed")

        self._tasks.append(asyncio.create_task(self._option_loop(settings.option_poll_seconds)))
        self._tasks.append(asyncio.create_task(self._signal_loop(settings.signal_eval_seconds)))
        self._tasks.append(asyncio.create_task(self._trade_loop(settings.signal_eval_seconds)))
        if settings.paper_trading:
            self._tasks.append(asyncio.create_task(self._paper_loop(settings.signal_eval_seconds)))
        if settings.broker_reconcile:
            self._tasks.append(asyncio.create_task(
                self._broker_reconcile_loop(settings.broker_reconcile_seconds)))

        if settings.news_active:
            self.news_service = NewsService(settings, news_store)
            self._tasks.append(asyncio.create_task(self._news_loop(settings.news_poll_seconds)))
            log.info("News intelligence enabled (model=%s)", settings.news_model)
        else:
            log.info("News intelligence off (set ANTHROPIC_API_KEY to enable)")

        self.running = True
        self._started_day = datetime.now(_IST).strftime("%Y-%m-%d")
        log.info(
            "Feed started: %d tokens, %d underlyings, signals on %s",
            len(tokens), len(universes), settings.signal_symbols,
        )

    @property
    def healthy(self) -> bool:
        """Running AND the ticker hasn't permanently given up."""
        return self.running and not market_state.ticker_dead

    @staticmethod
    def _tick_starved(max_silence_s: int = 180) -> bool:
        """Market open but no tick for `max_silence_s`. NIFTY/BANKNIFTY tick
        many times a second in session, so 3 minutes of silence is always a
        broken socket, never a quiet market."""
        from app.market import calendar as mcal

        if not mcal.is_market_open():
            return False
        age = market_state.last_tick_age()
        return age is None or age > max_silence_s

    async def supervisor(self, interval: float = 60.0) -> None:
        """Watchdog that owns feed recovery. Restarts the feed when:
          * the ticker permanently died (`on_noreconnect`) while the token is
            still believed valid — a restart builds a fresh ticker;
          * the IST day rolled over while running — expiries/ATM windows and
            the candle session are stale and must be re-resolved.
        If the token itself is dead, the restart path raises, `invalidate()`
        flips is_authenticated and the supervisor goes quiet until re-login.
        """
        failures = 0
        while True:
            await asyncio.sleep(interval)
            try:
                if not kite_service.is_authenticated:
                    failures = 0
                    continue
                if not self.running:
                    # A failed restart (which stops before starting) or a failed
                    # boot/post-login start must not end supervision for the day.
                    # Back off after repeated failures instead of going quiet.
                    failures += 1
                    if failures > 3 and failures % 5 != 0:
                        continue
                    log.warning("Supervisor: feed stopped while authenticated — attempting start")
                    await self.start()
                    failures = 0
                    continue
                today = datetime.now(_IST).strftime("%Y-%m-%d")
                if market_state.ticker_dead:
                    log.warning("Supervisor: ticker dead — attempting feed restart")
                    await self.restart()
                elif self._tick_starved():
                    # "Connected but silent": the WebSocket handshake can fail to
                    # complete while on_connect has already fired, so the socket
                    # reports connected and simply never delivers a tick. Seen
                    # live 2026-07-20 (28 min of silence, ticker_connected=True,
                    # no on_close, no on_noreconnect) — ticker_dead never fires,
                    # so tick age is the only reliable liveness signal.
                    age = market_state.last_tick_age()
                    log.warning("Supervisor: no ticks for %ss while market open — restarting feed", age)
                    await self.restart()
                elif self._started_day and self._started_day != today:
                    log.info("Supervisor: IST day rolled over — re-resolving universe")
                    await self.restart()
                failures = 0
            except Exception as exc:
                log.warning("Supervisor recovery attempt failed: %s", exc)

    async def _option_loop(self, interval: float) -> None:
        while True:
            try:
                if self.chain_builder is not None:
                    self.chain_builder.refresh_all()
            except Exception as exc:  # pragma: no cover
                log.warning("option refresh error: %s", exc)
            await asyncio.sleep(interval)

    async def _signal_loop(self, interval: float) -> None:
        # Let a first option snapshot land before evaluating.
        await asyncio.sleep(min(interval, 3))
        while True:
            try:
                if self.signal_service is not None:
                    self.signal_service.evaluate_all()
            except Exception as exc:  # pragma: no cover
                log.warning("signal loop error: %s", exc)
            await asyncio.sleep(interval)

    async def _paper_loop(self, interval: float) -> None:
        """Simulate entries on new signals and exits on plan triggers."""
        while True:
            try:
                if self.paper is not None:
                    from app.signals.store import signal_store
                    for symbol in get_settings().signal_symbols:
                        for mode in get_settings().signal_mode_list:
                            resp = signal_store.latest(symbol, mode)
                            if resp is not None and resp.signal is not None:
                                self.paper.consider(resp.signal)
                    self.paper.run_once()
            except Exception as exc:  # pragma: no cover
                log.warning("paper loop error: %s", exc)
            await asyncio.sleep(interval)

    async def _broker_reconcile_loop(self, interval: float) -> None:
        """Mirror the broker position book into the journal (read-only)."""
        while True:
            try:
                if self.trade_monitor is not None:
                    self.trade_monitor.reconcile_once()
            except Exception as exc:  # pragma: no cover
                log.warning("broker reconcile error: %s", exc)
            await asyncio.sleep(interval)

    async def _trade_loop(self, interval: float) -> None:
        while True:
            try:
                if self.trade_monitor is not None:
                    self.trade_monitor.run_once()
            except Exception as exc:  # pragma: no cover
                log.warning("trade loop error: %s", exc)
            await asyncio.sleep(interval)

    @staticmethod
    def _news_hours() -> bool:
        """Trading days 07:00–17:00 IST (holiday-aware via the shared calendar).
        The sentiment window is 6h, so off-session headlines can't affect a
        signal anyway — no reason to bill Claude for them."""
        from app.market import calendar as mcal
        return mcal.in_news_hours()

    async def _news_loop(self, interval: float) -> None:
        while True:
            try:
                if self.news_service is not None and self._news_hours():
                    # run_once blocks on network + Claude; keep it off the event loop
                    await asyncio.to_thread(self.news_service.run_once)
            except Exception as exc:  # pragma: no cover
                log.warning("news loop error: %s", exc)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        async with self._lifecycle:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            # Bounded: a task stuck in a blocking to_thread call (e.g. a news
            # fetch) can't be interrupted — don't hold a re-login hostage on it.
            await asyncio.wait(self._tasks, timeout=5)
        self._tasks.clear()
        if self.ticker is not None:
            self.ticker.stop()
        self.ticker = None
        self.chain_builder = None
        self.signal_service = None
        self.trade_monitor = None
        self.news_service = None
        self._token_in_use = None
        self.running = False


feed = FeedController()
