"""Runs the trade monitor over all active trades each cycle."""
from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.state import MarketState
from app.trades import monitor
from app.trades.store import TradeStore

log = logging.getLogger("tradewell.trades")


def _ist_minutes() -> int:
    return (int(time.time()) + 19800) % 86400 // 60


def _ist_date() -> str:
    t = time.gmtime(int(time.time()) + 19800)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


class TradeMonitorService:
    def __init__(self, state: MarketState, store: TradeStore) -> None:
        self.state = state
        self.store = store

    def _auto_close_set(self) -> set[str]:
        cfg = get_settings()
        if not cfg.auto_close_journal:
            return set()
        names = {n.strip().lower() for n in cfg.auto_close_triggers.split(",") if n.strip()}
        unknown = names - set(monitor.AUTO_CLOSE_NAMES)
        if unknown:
            log.warning("ignoring unknown AUTO_CLOSE_TRIGGERS: %s", ", ".join(sorted(unknown)))
        return names & set(monitor.AUTO_CLOSE_NAMES)

    def run_once(self) -> None:
        ist_min = _ist_minutes()
        ist_day = _ist_date()

        # Mutate live trades in place under the store lock so a concurrent
        # exit/partial (which runs on FastAPI's threadpool) can't be clobbered.
        def updater(trade) -> None:
            current = None
            if trade.token is not None:
                current = self.state.ticks.get(trade.token, {}).get("last_price")
            snap = self.state.underlying_snapshot(trade.symbol)
            spot = snap.ltp if snap else None
            monitor.evaluate(trade, current, spot, ist_min, ist_day)

        self.store.apply_monitor(updater)

        # Auto-close runs AFTER the monitor pass and outside the updater, not
        # inside it: apply_monitor holds the store lock while iterating the live
        # trades, and auto_close takes the same lock to write. Collecting the
        # decisions first, then acting, keeps that re-entrancy impossible.
        enabled = self._auto_close_set()
        if not enabled:
            return
        for trade in self.store.all():
            reason = monitor.auto_close_trigger(trade, enabled)
            if reason is None:
                continue
            px = trade.current_premium
            closed = self.store.auto_close(trade.id, float(px), reason)
            if closed is not None:
                log.info("auto-closed %s (%s) @ ₹%s — advisory only, no order placed",
                         trade.contract, reason, px)
