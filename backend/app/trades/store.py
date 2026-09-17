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
import re
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


_TICK = 0.05


def _round_tick(x: float) -> float:
    """Snap to the NSE Rs 0.05 tick, matching signals/risk.py so the card's
    levels and the trade's levels cannot drift by a rounding step."""
    return round(round(x / _TICK) * _TICK, 2)


_ENTERED_LOTS = re.compile(r"Entered\s+(\d+)\s+lot")


def _backfill_initial_quantity(t: Trade) -> None:
    """Recover the entry size on rows written before the field existed.

    `quantity` shrinks each time lots are booked, so on a partially-booked row
    it is the REMAINDER, while `realized_pnl` covers every leg. Dividing one by
    the other overstates the return badly — a real 10-lot row that booked four
    partials read -118.8%, which is impossible for a bought option.

    Rows with no partial are unambiguous: quantity never moved. Rows with one
    parse the lot count out of their own "Entered N lot(s)" event, which this
    codebase wrote itself. Anything unparseable is left as None so the UI shows
    no percentage rather than a wrong one.
    """
    if t.initial_quantity:
        return
    if not any(e.kind == "partial" for e in t.events):
        t.initial_quantity = t.quantity
        return
    for e in t.events:
        if e.kind != "entered":
            continue
        m = _ENTERED_LOTS.search(e.note or "")
        if m:
            qty = int(m.group(1)) * max(1, t.lot_size)
            if qty >= t.quantity:          # sanity: never smaller than what is left
                t.initial_quantity = qty
        return


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
                _backfill_initial_quantity(t)
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
        product: str | None = None, disaster_pct: float | None = None,
        quick_pct: float | None = None, sl_pct: float | None = None,
        rr1: float | None = None, rr2: float | None = None,
        notes: str | None = None,
    ) -> Trade:
        """Journal a position taken on `card`, with every level re-anchored to
        the fill.

        THE WHOLE LADDER COMES FROM `entry_premium`, not from the card. The card
        prices a plan against the premium at issue time; your fill is the only
        premium you actually own. On 22-Jul a card referenced at 197.45 was
        filled at 289.70 and inherited the card's stop verbatim — ₹9,847 at risk
        against a sizing note that said ₹3,845, and a 0.17 reward:risk on a
        setup advertised at 2.0. `disaster_sl` and `quick_target` were already
        computed from the fill; stop_loss/target1/target2 were the outliers, and
        mixing the two anchors is what put quick_target ABOVE target1 twice.

        `sl_pct`/`rr1`/`rr2` come from the mode profile. Without all three the
        card's own levels are kept (legacy callers and tests), and only the
        ordering invariant is enforced.
        """
        now = _now()
        events = [TradeEvent(ts=now, kind="entered", note=f"Entered {lots} lot(s) @ ₹{entry_premium}")]

        if sl_pct is not None and rr1 is not None and rr2 is not None:
            from app.signals.risk import price_ladder

            ladder = price_ladder(entry_premium, sl_pct, rr1, rr2, disaster_pct, quick_pct)
            stop_loss = ladder["premium_sl"]
            target1, target2 = ladder["target1"], ladder["target2"]
            disaster_sl, quick_target = ladder["disaster_sl"], ladder["quick_target"]
            # Only worth a note when the fill actually moved the plan.
            if abs(stop_loss - card.premium_sl) >= _TICK or abs(target1 - card.target1) >= _TICK:
                events.append(TradeEvent(
                    ts=now, kind="repriced",
                    note=(f"Levels re-priced to your ₹{entry_premium} fill — "
                          f"SL ₹{stop_loss} (card ₹{card.premium_sl}), "
                          f"T1 ₹{target1} (card ₹{card.target1})"),
                ))
        else:
            stop_loss, target1, target2 = card.premium_sl, card.target1, card.target2
            disaster_sl = (_round_tick(entry_premium * (1 - disaster_pct))
                           if disaster_pct else None)
            quick_target = (_round_tick(entry_premium * (1 + quick_pct))
                            if quick_pct else None)
            # price_ladder drops a T0 that is not actually earlier than T1; the
            # fallback path has to do the same or the monitor books "early" last.
            if quick_target is not None and quick_target >= target1:
                quick_target = None

        # Chasing is legal — you may have good reason — but it must be on the
        # record, because a fill above the zone is where the plan's arithmetic
        # stops describing the trade you are in.
        if card.entry_high and entry_premium > card.entry_high:
            over = (entry_premium / card.entry_high - 1) * 100
            events.append(TradeEvent(
                ts=now, kind="entry_outside_zone",
                note=(f"Filled ₹{entry_premium}, {over:.1f}% above the ₹{card.entry_low}–"
                      f"{card.entry_high} zone — the card's edge was priced lower"),
            ))

        with self._lock:
            tid = self._new_id()
            lots = max(1, lots)
            lot_size = max(1, lot_size)
            trade = Trade(
                id=tid, signal_id=card.id, symbol=card.symbol, mode=card.mode,
                direction=card.direction, contract=card.contract, strike=card.strike,
                expiry=card.expiry, token=card.token, entry_premium=entry_premium,
                lots=lots, lot_size=lot_size, quantity=lots * lot_size,
                initial_quantity=lots * lot_size,
                product=product,
                status=TradeStatus.ENTERED, stop_loss=stop_loss,
                target1=target1, target2=target2, trailing_sl=stop_loss,
                disaster_sl=disaster_sl,
                quick_target=quick_target,
                invalidation_level=card.invalidation_level, invalidation_dir=card.invalidation_dir,
                entry_score=card.confidence,
                tape_state=getattr(card, "tape_state", None),
                golden=getattr(card, "golden", None),
                macd_aligned=getattr(card, "macd_aligned", None),
                created_at=now, entered_at=now,
                # Set at creation (not a follow-up update) so a hollow tag is
                # ATOMIC with the fill — an untagged hollow row would be
                # miscounted as clean by is_hollow_row() in every aggregate.
                notes=notes,
                events=events,
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
    def apply_monitor(
        self, updater: Callable[[Trade], None], include_reversible: bool = False
    ) -> None:
        """Run `updater` over live rows; optionally also over same-day
        auto-closed rows. The reversible window exists because an auto-close is
        an INFERENCE that reopen can reverse — on 23-Jul a falsely-closed row
        went blind for 82 minutes and missed its own stop being breached at
        the tape low. Tracking through that window keeps the excursion record
        honest whichever way the close resolves.
        """
        def _reversible_today(t: Trade) -> bool:
            return (
                t.status is TradeStatus.EXITED
                and t.auto_closed
                and t.exited_at is not None
                and (t.exited_at + 19800) // 86400 == (_now() + 19800) // 86400
            )

        with self._lock:
            changed = False
            for t in self._trades.values():
                if t.status in _OPEN or (include_reversible and _reversible_today(t)):
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

    def set_stop(self, tid: str, stop_loss: float, note: str) -> Trade | None:
        """Re-anchor an open row's stop AND trailing floor to `stop_loss`.

        Exists for the stop-calibration paper twin (05-Aug), which must carry
        a calibrated stop that may sit LOOSER than the ladder's static one —
        update() deliberately ratchets trailing_sl upward only (max()), which
        is right for a live position and wrong for re-basing a just-created
        counterfactual. Not wired to any UI.
        """
        def fn(t: Trade) -> None:
            if t.status not in _OPEN:
                return  # a finalized journal record must not be edited
            t.stop_loss = stop_loss
            t.trailing_sl = stop_loss
            t.events.append(TradeEvent(ts=_now(), kind="updated", note=note))

        return self._apply(tid, fn)

    def auto_close(
        self, tid: str, exit_premium: float, reason: str,
        price_source: str = "estimated",
    ) -> Trade | None:
        """Close a row on a plan trigger, flagged as Tradewell's own doing.

        Distinct from `exit_trade` on purpose. That records a fill YOU report;
        this records what the plan (or the broker's book) says happened. The
        note states WHERE the price came from — the old template stamped
        "(estimated) · no order was placed" on every close, including
        broker-flat closes priced from Kite's own average sell fill, which
        taught the user to distrust numbers that were in fact real.
        `price_source`: "estimated" | "broker" | "simulated".
        """
        source_note = {
            "broker": "Kite day-average sell — your real fill",
            "simulated": "simulated fill, slippage applied",
        }.get(price_source, "estimated from the live premium — no order was placed by Tradewell")

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
            t.exit_price_source = price_source
            t.events.append(TradeEvent(
                ts=_now(), kind="auto_closed",
                note=(f"Auto-closed on {reason} @ ₹{exit_premium} ({source_note}) · "
                      f"P&L ₹{t.realized_pnl}"),
            ))

        return self._apply(tid, fn)

    def ack_invalidation(self, tid: str) -> Trade | None:
        """The user has seen the broken thesis and is holding by choice.

        Clears the sticky INVALIDATED recommendation (monitor treats ack newer
        than fired as acknowledged). A later re-break re-latches — the ack
        covers THIS break, not the concept of invalidation.
        """
        def fn(t: Trade) -> None:
            if t.invalidation_fired_at is None:
                return
            t.invalidation_ack_at = _now()
            t.events.append(TradeEvent(
                ts=_now(), kind="invalidation_ack",
                note="Invalidation acknowledged — holding against the thesis by explicit choice",
            ))

        return self._apply(tid, fn)

    def set_exit_reason(self, tid: str, reason: str) -> Trade | None:
        """Record WHY a closed trade ended, in the trader's words.

        Only meaningful on exited rows — an open trade has no exit to explain.
        Overwriting is allowed (second thoughts are data too); each answer is
        journaled so the history keeps both.
        """
        reason = reason.strip()
        if not reason:
            return None

        def fn(t: Trade) -> None:
            if t.status is not TradeStatus.EXITED:
                return
            t.exit_reason = reason[:120]
            t.events.append(TradeEvent(
                ts=_now(), kind="exit_reason",
                note=f"Exit reason recorded: {t.exit_reason}",
            ))

        t = self._apply(tid, fn)
        return t if t is not None and t.exit_reason == reason[:120] else None

    def note_reason_nudge(self, tid: str) -> None:
        """Journal that the one-time exit-reason reminder push was sent —
        the event IS the dedupe flag, so a restart can't re-nag."""
        def fn(t: Trade) -> None:
            t.events.append(TradeEvent(
                ts=_now(), kind="reason_nudge",
                note="Exit-reason reminder pushed to the phone",
            ))

        self._apply(tid, fn)

    def note_qty_mismatch(self, tid: str, held: int, journal_total: int | None = None) -> None:
        """Journal that the broker's quantity disagrees with the journal.

        The Jul-21 incident: two rows closed against one broker position with
        an invented P&L, unflagged, feeding the risk breakers. A mismatch is
        not automatically an error (partial manual exit, a second tranche
        outside Tradewell) — but it must be VISIBLE, not silent.
        `journal_total` is the summed quantity across sibling rows on the same
        instrument; the broker is compared against that, not one row.
        """
        def fn(t: Trade) -> None:
            total = journal_total if journal_total is not None else t.quantity
            t.events.append(TradeEvent(
                ts=_now(), kind="qty_mismatch",
                note=(f"Broker shows {held} qty against the journal's {total} for this "
                      "instrument — verify which position(s) the journal is describing"),
            ))

        self._apply(tid, fn)

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
            t.exit_price_source = None
            t.exit_reason = None         # the exit it explained never happened
            t.pnl = None
            # The close was FALSE, so the position was open the whole time —
            # the extremes observed during the reversible window belong to the
            # trade's real excursion record. Fold and clear.
            if t.post_close_mfe is not None and (
                t.mfe_premium is None or t.post_close_mfe > t.mfe_premium
            ):
                t.mfe_premium, t.mfe_at = t.post_close_mfe, t.post_close_mfe_at
            if t.post_close_mae is not None and (
                t.mae_premium is None or t.post_close_mae < t.mae_premium
            ):
                t.mae_premium, t.mae_at = t.post_close_mae, t.post_close_mae_at
            # The ladder folds under the same doctrine, with the REAL
            # observation timestamps; setdefault keeps any earlier genuine
            # stamp (a level touched in trade-life AND in the blind window
            # keeps the trade-life time — that IS the first touch).
            for key, ts_ in (t.post_close_touch_times or {}).items():
                t.touch_times.setdefault(key, ts_)
            t.post_close_touch_times = {}
            t.post_close_mfe = t.post_close_mfe_at = None
            t.post_close_mae = t.post_close_mae_at = None
            t.events.append(TradeEvent(ts=_now(), kind="reopened",
                                       note="Auto-close reversed — position still held"))

        with self._lock:
            t = self._trades.get(tid)
            if t is None or not t.auto_closed:
                return None
        return self._apply(tid, fn)

    def note_broker_qty(self, tid: str, qty: int | None) -> None:
        """Record what the broker's position book last showed for this row."""
        def fn(t: Trade) -> None:
            t.broker_qty = qty
            t.broker_checked_at = _now()

        self._apply(tid, fn)

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
