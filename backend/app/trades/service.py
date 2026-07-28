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


# Consecutive flat observations of the position book required before a
# broker-flat close. One flap of Kite's positions API produced two false
# closes and a lying P&L header on 23-Jul; at the ~15s reconcile cadence,
# three observations ≈ 30-45s of confirmed flatness.
_FLAT_CONFIRMS = 3


class TradeMonitorService:
    def __init__(self, state: MarketState, store: TradeStore) -> None:
        self.state = state
        self.store = store
        # Wired by the live feed only (same pattern as signal_store.notify):
        # tests constructing this service must never send phone pushes.
        self.notify = None
        self._flat_streak: dict[str, int] = {}
        self._qty_flagged: dict[str, int] = {}      # trade id -> last flagged broker qty
        self._inval_pushed: dict[str, tuple[int, float]] = {}  # id -> (fired_at, last push)

    def _push(self, title: str, body: str) -> None:
        if self.notify is not None:
            try:
                self.notify(title, body)
            except Exception:  # pragma: no cover - alerting must not break monitoring
                log.debug("trade notify failed", exc_info=True)

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
        cfg = get_settings()
        # The stall stop is the only trigger that can fire on a FROZEN price —
        # every other exit needs a level crossing a blackout can't produce. So
        # it is disarmed whenever the tick stream itself is stale: closing a
        # 10:45 trade at a premium last seen at 10:05 poisons the evidence.
        age = self.state.last_tick_age()
        fresh = age is not None and age <= max(cfg.feed_watchdog_age_s or 150, 60)
        stall = cfg.stall_exit_minutes if fresh else 0

        # Mutate live trades in place under the store lock so a concurrent
        # exit/partial (which runs on FastAPI's threadpool) can't be clobbered.
        # Same-day auto-closed rows keep excursion-only tracking: a reversible
        # close must not blind the record (23-Jul: an unobserved stop breach
        # during a false-close window).
        def updater(trade) -> None:
            current = None
            if trade.token is not None:
                current = self.state.ticks.get(trade.token, {}).get("last_price")
            if trade.status.value not in ("entered", "partial"):
                monitor.track_reversible(trade, current)
                return
            snap = self.state.underlying_snapshot(trade.symbol)
            spot = snap.ltp if snap else None
            sm = min(stall, 10) if (stall and trade.mode.value == "scalp") else stall
            monitor.evaluate(trade, current, spot, ist_min, ist_day, stall_minutes=sm,
                             early_derisk_pct=cfg.early_derisk_mfe_pct)

        self.store.apply_monitor(updater, include_reversible=True)
        self._push_unacked_invalidations()
        self._nudge_missing_exit_reasons()

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

    def _nudge_missing_exit_reasons(self) -> None:
        """One reminder push per broker-flat close left unclassified ~15 min.

        The 'why did you exit?' prompt ships in the Journal tab but the 28-Jul
        audit found it 9-for-9 unanswered — nothing ever NUDGED, and with
        startup pings gone the phone only ever hears about signals. One push
        per trade, ever (the reason_nudge event is the dedupe flag), and only
        while the question is still open."""
        import time as _t

        if self.notify is None:
            return
        now = _t.time()
        for t in self.store.all():
            if t.status.value != "exited" or t.auto_close_reason != "broker flat":
                continue
            if getattr(t, "exit_reason", None):
                continue
            if not t.exited_at or now - t.exited_at < 900:
                continue                 # give the dashboard prompt first shot
            # 24h upper bound: at deploy time the journal already held NINE
            # unreasoned broker-flat rows from previous days — without this,
            # the first monitor cycle would burst-push the entire backlog.
            # Old rows stay visible in the Journal prompt; the phone only
            # hears about today's.
            if now - t.exited_at > 86400:
                continue
            if any(e.kind == "reason_nudge" for e in t.events):
                continue
            pnl = t.realized_pnl or 0.0
            self._push(
                "Why did you exit?",
                (f"{t.contract} closed at the broker ({'+' if pnl >= 0 else ''}"
                 f"₹{pnl:,.0f}) and has no exit reason yet. One tap in the "
                 "Journal tab — broker stop / target / fear / better setup — "
                 "teaches the exit report what actually happened."),
            )
            self.store.note_reason_nudge(t.id)

    def _push_unacked_invalidations(self) -> None:
        """Phone the broken thesis, and keep phoning every 10 minutes until the
        user exits or acknowledges. One silent banner was ignorable; the 23-Jul
        hold went 93 minutes through two alerts nobody had to look at."""
        import time as _t

        now = _t.time()
        for t in self.store.all():
            if t.status.value not in ("entered", "partial"):
                self._inval_pushed.pop(t.id, None)
                continue
            if not monitor.invalidation_unacked(t):
                self._inval_pushed.pop(t.id, None)
                continue
            fired = t.invalidation_fired_at or 0
            prev = self._inval_pushed.get(t.id)
            if prev is None and now - fired > 60:
                # Fresh service instance (feed restarts rebuild it daily) seeing
                # an OLD unacked break: the pre-restart instance already paged
                # it. Seed the map silently so the restart itself doesn't
                # duplicate the nag; the 10-minute cadence resumes from here.
                self._inval_pushed[t.id] = (fired, now)
                continue
            if prev is None or prev[0] != fired or now - prev[1] >= 600:
                self._push(
                    "TRADEWELL INVALIDATION",
                    f"{t.contract}: thesis broke ({t.symbol} vs {t.invalidation_level:.0f}) "
                    "and is unacknowledged — exit, or acknowledge in the journal to hold.",
                )
                self._inval_pushed[t.id] = (fired, now)

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

        all_rows = self.store.all()
        open_rows = [t for t in all_rows if t.status.value in ("entered", "partial")]
        # Which open rows share one broker instrument: needed both for the
        # quantity cross-check and for closing OLDEST-FIRST — the Jul-21 bug
        # closed two rows against one position in the same second, inventing
        # one of the P&Ls. Token-only on purpose: reconcile.match treats a
        # missing product as a wildcard, so a legacy product-None row and an
        # MIS row on the same token CAN both match one broker position — they
        # must be siblings here too, or they double-close in one pass.
        def _sibling_key(t):
            return t.token or t.contract

        def _older_sibling_exists(trade, siblings):
            # Total order: entered_at ties (1s resolution, double-click
            # entries) broken by id — strict '<' alone let both rows close in
            # the same pass at the same price, rebuilding the Jul-21 incident.
            me = (trade.entered_at, trade.id)
            return any((x.entered_at, x.id) < me for x in siblings if x.id != trade.id)

        for trade in all_rows:
            pos = reconcile.match(trade, positions)
            held = pos.quantity if pos is not None else 0
            open_row = trade.status.value in ("entered", "partial")

            if open_row:
                if held:
                    self._flat_streak.pop(trade.id, None)
                    self.store.note_broker_qty(trade.id, held)
                    # Quantity truth: the broker's number disagreeing with the
                    # journal is not an error, but it must never be silent.
                    # Siblings legitimately split one position, so the check
                    # compares the broker against their SUM — the Jul-21
                    # incident (two rows, one position) is by definition the
                    # multi-row case, and a sole-row-only check was blind
                    # exactly there. Flagged on the oldest sibling, deduped
                    # against the persisted journal so feed restarts (which
                    # rebuild this service daily) don't re-append the event.
                    siblings = [x for x in open_rows if _sibling_key(x) == _sibling_key(trade)]
                    total = sum(x.quantity for x in siblings)
                    if held != total and not _older_sibling_exists(trade, siblings):
                        already = any(
                            e.kind == "qty_mismatch" and f"Broker shows {held} qty" in e.note
                            for e in trade.events
                        )
                        if not already and self._qty_flagged.get(trade.id) != held:
                            self.store.note_qty_mismatch(trade.id, held, total)
                            self._qty_flagged[trade.id] = held
                    continue
                # Absent or flat. Only meaningful once we have SEEN it there:
                # otherwise a trade marked entered before the buy actually fills
                # would be closed instantly.
                if trade.broker_qty:
                    # DEBOUNCED: one flap of the positions API must not close a
                    # row. Require _FLAT_CONFIRMS consecutive flat sightings.
                    streak = self._flat_streak.get(trade.id, 0) + 1
                    self._flat_streak[trade.id] = streak
                    if streak < _FLAT_CONFIRMS:
                        log.info("broker flat for %s (%d/%d) — awaiting confirmation",
                                 trade.contract, streak, _FLAT_CONFIRMS)
                        continue


                    # Oldest first, one per pass: sibling rows on the same
                    # instrument must not all book the same exit in one second.
                    siblings = [x for x in open_rows if _sibling_key(x) == _sibling_key(trade)]
                    if _older_sibling_exists(trade, siblings):
                        continue
                    # Kite's own average sell price is the real fill — far
                    # better than the live premium we would otherwise guess.
                    sell_px = pos.sell_price if pos and pos.sell_price else None
                    px = sell_px or trade.current_premium
                    if px:
                        self.store.auto_close(
                            trade.id, float(px), "broker flat",
                            price_source="broker" if sell_px else "estimated")
                        self.store.note_broker_qty(trade.id, 0)
                        self._flat_streak.pop(trade.id, None)
                        log.info("journal closed from broker book: %s @ ₹%s", trade.contract, px)
                        self._warn_resting_stop(trade)
            elif trade.auto_closed and held:
                # We closed this on price, but Kite says you are still in it.
                # The broker wins.
                if self.store.reopen(trade.id) is not None:
                    self.store.note_broker_qty(trade.id, held)
                    log.warning("reopened %s — broker still shows %s qty", trade.contract, held)
        return True

    def _warn_resting_stop(self, trade) -> None:
        """A broker-flat close after a Kite stop hand-off leaves a live SELL
        order Tradewell cannot see or cancel. If it triggers with no long
        behind it, it opens a naked SHORT — the one accidental-short path
        still open. Journal it and phone it."""
        handoffs = [e for e in trade.events if e.kind == "stop_handoff"]
        if not handoffs:
            return
        note = (f"Position closed at the broker, but a Kite SL SELL was handed off earlier "
                f"({handoffs[-1].note}). If that order is still resting, CANCEL IT in Kite — "
                "triggering with no long behind it opens a short.")
        def fn(t) -> None:
            from app.trades.models import TradeEvent
            import time as _t
            t.events.append(TradeEvent(ts=int(_t.time()), kind="resting_stop_warning", note=note))
        self.store._apply(trade.id, fn)
        self._push("TRADEWELL: CANCEL RESTING STOP", f"{trade.contract} — {note}")
