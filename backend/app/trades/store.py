"""Trade store: in-memory + JSON-file persistence.

The journal must survive restarts (a lost trade log is worse than a lost signal),
so trades are persisted to a local JSON file after every mutation, written
atomically (temp file + os.replace). Postgres replaces this in a later phase
behind the same accessors.

Concurrency: the monitor loop runs on the asyncio loop, but FastAPI dispatches
our sync route handlers to a worker threadpool — so mutations and monitoring run
on different threads. Every read-modify-write happens under ``_lock`` (including
``apply_monitor``, which mutates the *live* trades in place rather than
overwriting them with a stale snapshot), so a concurrent exit/partial can never
be clobbered.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from app.signals.models import SignalCard
from app.trades.models import Trade, TradeAction, TradeEvent, TradeStatus

log = logging.getLogger("tradewell.trades")

# Anchored to the backend directory (not CWD): launching uvicorn from another
# directory must not silently open an empty journal.
_STORE_PATH = Path(__file__).resolve().parents[2] / ".trades.json"
_OPEN = (TradeStatus.ENTERED, TradeStatus.PARTIAL)


def _now() -> int:
    return int(time.time())


class TradeStore:
    def __init__(self, path: Path = _STORE_PATH) -> None:
        self._lock = threading.Lock()
        self._trades: dict[str, Trade] = {}
        self._orphans: list[dict] = []   # records we couldn't parse — never dropped
        self._path = path
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            # Preserve the unreadable file — the next _save would otherwise
            # permanently overwrite whatever journal history it still holds.
            backup = self._path.with_name(self._path.name + ".corrupt")
            log.warning("trades file unreadable; preserving it as %s", backup.name)
            try:
                self._path.replace(backup)
            except Exception:  # pragma: no cover
                pass
            return
        for raw in data:
            try:  # one bad record must not drop the rest
                t = Trade.model_validate(raw)
                self._trades[t.id] = t
            except Exception:
                log.warning("unparseable trade record kept as orphan (won't be lost on save)")
                self._orphans.append(raw)

    def _save(self) -> None:
        """Atomic write; raises on failure so callers can surface it."""
        payload = [t.model_dump(mode="json") for t in self._trades.values()] + self._orphans
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)

    def _new_id(self) -> str:
        while True:
            tid = "T-" + uuid.uuid4().hex
            if tid not in self._trades:
                return tid

    # ---- create ----
    def create_from_signal(
        self, card: SignalCard, lots: int, entry_premium: float, lot_size: int,
        product: str | None = None,
    ) -> Trade:
        now = _now()
        with self._lock:
            tid = self._new_id()
            lots = max(1, lots)
            lot_size = max(1, lot_size)
            trade = Trade(
                id=tid, signal_id=card.id, symbol=card.symbol, mode=card.mode,
                direction=card.direction, contract=card.contract, strike=card.strike,
                expiry=card.expiry, token=card.token, entry_premium=entry_premium,
                lots=lots, lot_size=lot_size, quantity=lots * lot_size,
                product=product,
                status=TradeStatus.ENTERED, stop_loss=card.premium_sl,
                target1=card.target1, target2=card.target2, trailing_sl=card.premium_sl,
                invalidation_level=card.invalidation_level, invalidation_dir=card.invalidation_dir,
                created_at=now, entered_at=now,
                events=[TradeEvent(ts=now, kind="entered", note=f"Entered {lots} lot(s) @ ₹{entry_premium}")],
            )
            self._trades[tid] = trade
            self._save()
            return trade.model_copy(deep=True)

    # ---- reads (deep copies; callers must not mutate the store) ----
    def get(self, tid: str) -> Trade | None:
        with self._lock:
            t = self._trades.get(tid)
            return t.model_copy(deep=True) if t else None

    def all(self) -> list[Trade]:
        with self._lock:
            trades = list(self._trades.values())
            active_first = sorted(
                trades,
                key=lambda t: (t.status not in _OPEN, -t.created_at),
            )
            return [t.model_copy(deep=True) for t in active_first]

    # ---- monitoring (mutates live trades under the lock) ----
    def apply_monitor(self, updater: Callable[[Trade], None]) -> None:
        with self._lock:
            changed = False
            for t in self._trades.values():
                if t.status in _OPEN:
                    # Persist only on actual change — off-hours the monitor is a
                    # no-op and rewriting the journal every cycle (~4,600 disk
                    # writes/day) buys nothing.
                    before = t.model_dump(mode="json")
                    updater(t)
                    if not changed and t.model_dump(mode="json") != before:
                        changed = True
            if changed:
                try:
                    self._save()
                except Exception as exc:  # monitor persistence failure shouldn't kill the loop
                    log.warning("trade persist (monitor) failed: %s", exc)

    # ---- mutations ----
    def _apply(self, tid: str, fn: Callable[[Trade], None]) -> Trade | None:
        with self._lock:
            t = self._trades.get(tid)
            if t is None:
                return None
            fn(t)
            self._save()
            return t.model_copy(deep=True)

    def exit_trade(self, tid: str, exit_premium: float) -> Trade | None:
        def fn(t: Trade) -> None:
            if t.status not in _OPEN:
                return
            realized = round((exit_premium - t.entry_premium) * t.quantity, 2)
            t.realized_pnl = round(t.realized_pnl + realized, 2)
            t.exit_premium = exit_premium
            t.pnl = t.realized_pnl
            t.status = TradeStatus.EXITED
            t.exited_at = _now()
            t.recommendation = TradeAction.EXIT
            t.recommendation_note = "Closed"
            t.events.append(TradeEvent(ts=_now(), kind="exited",
                                       note=f"Exited @ ₹{exit_premium} · P&L ₹{t.realized_pnl}"))

        return self._apply(tid, fn)

    def book_partial(self, tid: str, exit_premium: float, fraction: float) -> Trade | None:
        """Book whole lots. Returns None if the trade is missing/closed or nothing
        can be booked (e.g. a single-lot position) so the route can 409."""
        with self._lock:
            t = self._trades.get(tid)
            if t is None or t.status not in _OPEN:
                return None
            frac = min(0.9, max(0.1, fraction))
            book_lots = max(1, int(t.lots * frac))
            if book_lots >= t.lots:
                return None  # can't keep a remainder running — use Exit instead
            book_qty = book_lots * t.lot_size
            realized = round((exit_premium - t.entry_premium) * book_qty, 2)
            t.realized_pnl = round(t.realized_pnl + realized, 2)
            t.lots -= book_lots
            t.quantity = t.lots * t.lot_size
            t.status = TradeStatus.PARTIAL
            if t.stop_loss < t.entry_premium:
                t.stop_loss = t.entry_premium
            t.events.append(TradeEvent(ts=_now(), kind="partial",
                                       note=f"Booked {book_lots} lot(s) @ ₹{exit_premium} (₹{realized}); SL to entry"))
            self._save()
            return t.model_copy(deep=True)

    def update(self, tid: str, stop_loss=None, target1=None, target2=None, notes=None) -> Trade | None:
        def fn(t: Trade) -> None:
            if t.status not in _OPEN:
                return  # a finalized journal record must not be edited by a stray click
            if stop_loss is not None:
                t.stop_loss = stop_loss
                t.trailing_sl = max(t.trailing_sl, stop_loss)
            if target1 is not None:
                t.target1 = target1
            if target2 is not None:
                t.target2 = target2
            if notes is not None:
                t.notes = notes
            t.events.append(TradeEvent(ts=_now(), kind="updated", note="Manual update"))

        return self._apply(tid, fn)

    def auto_close(self, tid: str, exit_premium: float, reason: str) -> Trade | None:
        """Close a row on a plan trigger, flagged as Tradewell's own doing.

        Distinct from `exit_trade` on purpose. That records a fill YOU report;
        this records what the plan says should have happened, at the live
        premium seen at detection. No order was placed, so the price is an
        estimate of your exit and the row stays reversible via `reopen`.
        """
        def fn(t: Trade) -> None:
            if t.status not in _OPEN:
                return
            realized = round((exit_premium - t.entry_premium) * t.quantity, 2)
            t.realized_pnl = round(t.realized_pnl + realized, 2)
            t.exit_premium = exit_premium
            t.pnl = t.realized_pnl
            t.status = TradeStatus.EXITED
            t.exited_at = _now()
            t.auto_closed = True
            t.auto_close_reason = reason
            t.events.append(TradeEvent(
                ts=_now(), kind="auto_closed",
                note=(f"Auto-closed on {reason} @ ₹{exit_premium} (estimated) · "
                      f"P&L ₹{t.realized_pnl} — no order was placed"),
            ))

        return self._apply(tid, fn)

    def reopen(self, tid: str, exit_premium: float | None = None) -> Trade | None:
        """Undo an auto-close: you are still holding, or filled elsewhere.

        Only auto-closed rows may be reopened — a fill you reported yourself is
        a fact, and quietly reversing it would corrupt the journal.
        """
        def fn(t: Trade) -> None:
            if not t.auto_closed or t.status is not TradeStatus.EXITED:
                return
            # Roll back exactly what auto_close booked.
            realized = round((t.exit_premium - t.entry_premium) * t.quantity, 2) if t.exit_premium else 0.0
            t.realized_pnl = round(t.realized_pnl - realized, 2)
            # Back to PARTIAL only if lots were genuinely booked earlier — a
            # residual realized_pnl of 0.00 is not proof either way.
            booked = any(e.kind == "partial" for e in t.events)
            t.status = TradeStatus.PARTIAL if booked else TradeStatus.ENTERED
            t.exited_at = None
            t.exit_premium = None
            t.auto_closed = False
            t.auto_close_reason = None
            t.pnl = None
            t.events.append(TradeEvent(ts=_now(), kind="reopened",
                                       note="Auto-close reversed — position still held"))

        with self._lock:
            t = self._trades.get(tid)
            if t is None or not t.auto_closed:
                return None
        return self._apply(tid, fn)

    def note_stop_handoff(self, tid: str, trigger: float) -> int:
        """Record that a protective stop was handed to Kite; return prior count.

        Tradewell cannot see the user's Kite order book, so this journal entry
        is the only memory that a stop was already sent. It matters because a
        SECOND stop for the same position means two SELL orders against one
        long: the first closes it, the second opens a short.
        """
        prior = 0
        with self._lock:
            t = self._trades.get(tid)
            if t is None:
                return 0
            prior = sum(1 for e in t.events if e.kind == "stop_handoff")
            t.events.append(TradeEvent(
                ts=_now(), kind="stop_handoff",
                note=f"Stop-loss order handed to Kite at trigger ₹{trigger:.2f}",
            ))
            self._save()
        return prior

    def ignore(self, tid: str) -> Trade | None:
        def fn(t: Trade) -> None:
            if t.status not in _OPEN:
                return  # exited/ignored trades keep their status and exit time
            t.status = TradeStatus.IGNORED
            t.exited_at = _now()
            t.events.append(TradeEvent(ts=_now(), kind="ignored", note="Dismissed"))

        return self._apply(tid, fn)


trade_store = TradeStore()
