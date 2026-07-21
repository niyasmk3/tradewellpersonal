"""Runs the trade monitor over all active trades each cycle."""
from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.state import MarketState
from app.trades import monitor, reconcile
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
            # A row the broker has CONFIRMED is governed by the broker, not by
            # the tape. Closing it on price while Kite still shows it open would
            # just be undone by the next reconcile pass, and the two would
            # flip-flop forever. Price inference is the fallback, not the rule.
            if trade.broker_qty is not None:
                continue
            reason = monitor.auto_close_trigger(trade, enabled)
            if reason is None:
                continue
            px = trade.current_premium
            closed = self.store.auto_close(trade.id, float(px), reason)
            if closed is not None:
                log.info("auto-closed %s (%s) @ ₹%s — advisory only, no order placed",
                         trade.contract, reason, px)

    def reconcile_once(self) -> bool:
        """Mirror Zerodha's position book into the journal. READ-ONLY.

        Returns True if the book was actually read. A failed call returns False
        and changes nothing — treating "we could not ask" as "you hold nothing"
        would close every open row at once.
        """
        cfg = get_settings()
        if not cfg.broker_reconcile:
            return False
        from app.kite.client import kite_service

        positions = reconcile.fetch_broker_positions(kite_service.kite)
        if positions is None:
            return False

        for trade in self.store.all():
            pos = reconcile.match(trade, positions)
            held = pos.quantity if pos is not None else 0
            open_row = trade.status.value in ("entered", "partial")

            if open_row:
                if held:
                    self.store.note_broker_qty(trade.id, held)
                    continue
                # Absent or flat. Only meaningful once we have SEEN it there:
                # otherwise a trade marked entered before the buy actually fills
                # would be closed instantly.
                if trade.broker_qty:
                    # Kite's own average sell price is the real fill — far
                    # better than the live premium we would otherwise guess.
                    px = (pos.sell_price if pos and pos.sell_price else None) or trade.current_premium
                    if px:
                        self.store.auto_close(trade.id, float(px), "broker flat")
                        self.store.note_broker_qty(trade.id, 0)
                        log.info("journal closed from broker book: %s @ ₹%s", trade.contract, px)
            elif trade.auto_closed and held:
                # We closed this on price, but Kite says you are still in it.
                # The broker wins.
                if self.store.reopen(trade.id) is not None:
                    self.store.note_broker_qty(trade.id, held)
                    log.warning("reopened %s — broker still shows %s qty", trade.contract, held)
        return True
