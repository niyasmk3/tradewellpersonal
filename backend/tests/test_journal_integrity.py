"""P1 journal-integrity behaviours (24-Jul review closures).

Every test here pins a failure that actually happened:
  * one flap of Kite's positions API produced two false closes and a lying
    P&L header (debounce);
  * two rows closed against one position in one second with an invented P&L
    (oldest-first + quantity flags);
  * an unobserved stop breach while a row sat falsely closed (reversible
    tracking);
  * advice silently reverting to "hold" after ignored invalidation alerts
    (sticky ack);
  * "(estimated) · no order was placed" stamped on real broker fills
    (source-aware notes).

Run:  python backend/tests/test_journal_integrity.py
"""
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.signals.models import Direction, TradingMode
from app.trades import monitor
from app.trades import service as trade_service_mod
from app.trades.models import Trade, TradeAction, TradeStatus
from app.trades.reconcile import BrokerPosition
from app.trades.service import TradeMonitorService, _FLAT_CONFIRMS
from app.trades.store import TradeStore

NOW = int(time.time())


def _store(name="/tmp/tw-integrity-test.json"):
    p = pathlib.Path(name)
    p.unlink(missing_ok=True)
    return TradeStore(path=p)


def _trade(store: TradeStore, **over) -> Trade:
    base = dict(
        id="T-x", symbol="NIFTY", mode=TradingMode.INTRADAY, direction=Direction.CE,
        contract="NIFTY 24350 CE", strike=24350, token=999, product="MIS",
        entry_premium=120.0, lots=1, lot_size=75, quantity=75,
        status=TradeStatus.ENTERED, stop_loss=100.0, target1=150.0, target2=180.0,
        trailing_sl=100.0, invalidation_level=24300.0, invalidation_dir="above",
        created_at=NOW, entered_at=NOW,
    )
    base.update(over)
    t = Trade(**base)
    with store._lock:
        store._trades[t.id] = t
        store._save()
    return t


class _State:
    def __init__(self, px=120.0, age=5):
        self.ticks = {999: {"last_price": px, "ts": int(time.time()) - 2}}
        self._age = age

    def last_tick_age(self):
        return self._age

    def underlying_snapshot(self, sym):
        return type("S", (), {"ltp": 24350.0})()


class _FakePositions:
    """Injectable position book: a list per call, cycled."""
    def __init__(self, sequences):
        self.seq = list(sequences)

    def next(self):
        return self.seq.pop(0) if self.seq else []


def _svc(store, positions_sequences, px=120.0):
    svc = TradeMonitorService(_State(px=px), store)
    fake = _FakePositions(positions_sequences)
    trade_service_mod.reconcile.fetch_broker_positions = lambda kite: fake.next()
    return svc


_REAL_FETCH = trade_service_mod.reconcile.fetch_broker_positions


def _pos(qty, sell_price=None, token=999):
    return [BrokerPosition(tradingsymbol="NIFTY24350CE", token=token, product="MIS",
                           quantity=qty, sell_quantity=75 if sell_price else 0,
                           sell_price=sell_price)]


def test_broker_flat_is_debounced():
    """One flap must close nothing; sustained flatness closes after N sightings."""
    try:
        s = _store()
        t = _trade(s)
        seqs = [_pos(75)] + [_pos(0, sell_price=141.0)] * _FLAT_CONFIRMS
        svc = _svc(s, seqs)
        svc.reconcile_once()                       # seen holding
        svc.reconcile_once()                       # flat 1/N — a flap
        assert s.get(t.id).status is TradeStatus.ENTERED, "closed on a single flap"
        for _ in range(_FLAT_CONFIRMS - 1):
            svc.reconcile_once()
        assert s.get(t.id).status is TradeStatus.EXITED
        print(f"  INTEG  -> flat close only after {_FLAT_CONFIRMS} consecutive sightings")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_flat_streak_resets_when_position_reappears():
    try:
        s = _store()
        t = _trade(s)
        seqs = [_pos(75), _pos(0), _pos(0), _pos(75), _pos(0), _pos(0)]
        svc = _svc(s, seqs)
        for _ in range(6):
            svc.reconcile_once()
        assert s.get(t.id).status is TradeStatus.ENTERED, "flap sequence closed the row"
        print("  INTEG  -> reappearing position resets the flat streak")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_broker_close_uses_source_aware_note():
    try:
        s = _store()
        t = _trade(s)
        seqs = [_pos(75)] + [_pos(0, sell_price=141.0)] * _FLAT_CONFIRMS
        svc = _svc(s, seqs)
        for _ in range(_FLAT_CONFIRMS + 1):
            svc.reconcile_once()
        closed = s.get(t.id)
        note = closed.events[-1].note
        assert "Kite day-average sell" in note, note
        assert "no order was placed" not in note, note
        print("  INTEG  -> broker-priced close says so; no false 'estimated' stamp")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_siblings_close_oldest_first_never_together():
    """Two rows, one broker position going flat: one close per pass, oldest
    first — never both in the same second at the same price."""
    try:
        s = _store()
        _trade(s, id="T-old", entered_at=NOW - 600)
        _trade(s, id="T-new", entered_at=NOW - 60)
        seqs = [_pos(150)] + [_pos(0, sell_price=141.0)] * (_FLAT_CONFIRMS + 2)
        svc = _svc(s, seqs)
        svc.reconcile_once()                       # both seen holding
        for _ in range(_FLAT_CONFIRMS):
            svc.reconcile_once()
        old, new = s.get("T-old"), s.get("T-new")
        assert old.status is TradeStatus.EXITED and new.status is TradeStatus.ENTERED, \
            (old.status, new.status)
        svc.reconcile_once()                       # next pass takes the sibling
        assert s.get("T-new").status is TradeStatus.EXITED
        print("  INTEG  -> sibling rows close oldest-first, one per pass")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_quantity_mismatch_is_flagged_once():
    try:
        s = _store()
        t = _trade(s, quantity=75, initial_quantity=75)
        svc = _svc(s, [_pos(650), _pos(650)])
        svc.reconcile_once()
        svc.reconcile_once()                       # same qty again: no repeat spam
        events = [e for e in s.get(t.id).events if e.kind == "qty_mismatch"]
        assert len(events) == 1, [e.note for e in events]
        assert "650" in events[0].note and "75" in events[0].note
        print("  INTEG  -> broker 650 vs journal 75: flagged exactly once")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_reversible_rows_record_separately_and_fold_on_reopen():
    """An auto-closed (reversible) row must not go blind — but its post-close
    observations must not contaminate the trade-life excursion evidence either.
    They live in post_close_* and fold into mfe/mae ONLY on reopen (the close
    was false, so the trade was open the whole time)."""
    s = _store()
    t = _trade(s)
    # A trade-life MAE exists before the close.
    monitor.evaluate(s.get(t.id), 110.0, 24360.0, 600)
    def seed_mae(tr):
        monitor.evaluate(tr, 110.0, 24360.0, 600)
    s.apply_monitor(seed_mae)
    s.auto_close(t.id, 118.0, "broker flat", price_source="broker")
    row = s.get(t.id)
    assert row.auto_closed and row.status is TradeStatus.EXITED
    assert row.exit_price_source == "broker"

    def updater(tr):
        if tr.status.value not in ("entered", "partial"):
            monitor.track_reversible(tr, 84.10)

    s.apply_monitor(updater, include_reversible=True)
    row = s.get(t.id)
    assert row.post_close_mae == 84.10, row.post_close_mae
    assert row.mae_premium == 110.0, "trade-life MAE must stay uncontaminated"
    # Without the flag the row is skipped — paper keeps its old semantics.
    def poison(tr):
        monitor.track_reversible(tr, 1.0)
    s.apply_monitor(poison)                       # include_reversible defaults False
    assert s.get(t.id).post_close_mae == 84.10
    # Reopen: the close was false → the observation belongs to the trade.
    assert s.reopen(t.id) is not None
    row = s.get(t.id)
    assert row.mae_premium == 84.10 and row.post_close_mae is None
    print("  INTEG  -> post-close extremes separated; folded into MAE only on reopen")


def test_ack_holds_through_a_continuous_breach():
    """THE critical review case: acknowledge while spot is STILL broken — the
    exact hold-by-choice scenario. A level-triggered latch nullified the ack
    within one cycle; edge-triggering must make it stick."""
    s = _store()
    t = _trade(s)
    monitor.evaluate(t, 118.0, 24250.0, 600)          # break fires
    assert t.recommendation is TradeAction.INVALIDATED
    t.invalidation_fired_at -= 5                       # let the ack be strictly newer
    with s._lock:
        s._trades[t.id] = t
        s._save()
    s.ack_invalidation(t.id)
    acked = s.get(t.id)
    for _ in range(5):                                 # breach persists across cycles
        monitor.evaluate(acked, 118.0, 24250.0, 600)
    assert acked.recommendation is TradeAction.INVALIDATED  # still advising exit…
    assert not monitor.invalidation_unacked(acked), "ack was nullified by re-latch"
    # Recovery observed, then a NEW break → a fresh episode fires and nags.
    monitor.evaluate(acked, 121.0, 24360.0, 600)
    assert acked.recommendation is TradeAction.HOLD
    monitor.evaluate(acked, 118.0, 24250.0, 600)
    assert monitor.invalidation_unacked(acked), "new episode after recovery must latch"
    print("  INTEG  -> ack survives a continuous breach; recovery+re-break re-latches")


def test_same_second_siblings_never_close_together():
    """entered_at has 1s resolution; a tie must not defeat oldest-first."""
    try:
        s = _store()
        _trade(s, id="T-a", entered_at=NOW - 300)
        _trade(s, id="T-b", entered_at=NOW - 300)      # same second
        seqs = [_pos(150)] + [_pos(0, sell_price=141.0)] * (_FLAT_CONFIRMS + 2)
        svc = _svc(s, seqs)
        svc.reconcile_once()
        for _ in range(_FLAT_CONFIRMS):
            svc.reconcile_once()
        states = sorted((s.get("T-a").status.value, s.get("T-b").status.value))
        assert states == ["entered", "exited"], states
        svc.reconcile_once()
        assert s.get("T-a").status is TradeStatus.EXITED
        assert s.get("T-b").status is TradeStatus.EXITED
        print("  INTEG  -> same-second siblings close one per pass, id tie-break")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_multi_row_qty_mismatch_uses_the_sum():
    """The Jul-21 configuration IS the multi-row case: broker 75 against two
    rows of 75 each must flag; two rows legitimately splitting 150 must not."""
    try:
        s = _store()
        _trade(s, id="T-a", entered_at=NOW - 300, quantity=75)
        _trade(s, id="T-b", entered_at=NOW - 200, quantity=75)
        svc = _svc(s, [_pos(75), _pos(75), _pos(150), _pos(150)])
        svc.reconcile_once()                           # 75 vs 150 → flag on oldest
        svc.reconcile_once()                           # same qty → no repeat
        flags_a = [e for e in s.get("T-a").events if e.kind == "qty_mismatch"]
        flags_b = [e for e in s.get("T-b").events if e.kind == "qty_mismatch"]
        assert len(flags_a) == 1 and not flags_b, (len(flags_a), len(flags_b))
        assert "75" in flags_a[0].note and "150" in flags_a[0].note
        svc2 = _svc(s, [])                             # legit split: broker 150
        trade_service_mod.reconcile.fetch_broker_positions = lambda kite: _pos(150)
        svc2.reconcile_once()
        assert len([e for e in s.get("T-a").events if e.kind == "qty_mismatch"]) == 1
        print("  INTEG  -> broker-vs-SUM flagged once on the oldest; legit split silent")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_restart_does_not_duplicate_qty_flags_or_nags():
    """Feed restarts rebuild the service daily; persisted-journal dedup must
    survive the fresh instance."""
    try:
        s = _store()
        t = _trade(s, quantity=75)
        svc1 = _svc(s, [_pos(650)])
        svc1.reconcile_once()
        svc2 = _svc(s, [_pos(650)])                    # "restart"
        svc2.reconcile_once()
        flags = [e for e in s.get(t.id).events if e.kind == "qty_mismatch"]
        assert len(flags) == 1, len(flags)
        # Old unacked invalidation + fresh service: seeded silently, no push.
        # time.time() at RUNTIME, not module NOW: test_paper pins the shared
        # time module to a fixed midday for the whole suite, and the service
        # ages the break against that same (patched) clock.
        pushes = []
        t2 = s.get(t.id)
        t2.invalidation_fired_at = int(time.time()) - 3600
        with s._lock:
            s._trades[t2.id] = t2
            s._save()
        svc3 = _svc(s, [])
        svc3.notify = lambda title, body: pushes.append(title)
        svc3._push_unacked_invalidations()
        assert pushes == [], pushes
        print("  INTEG  -> restart: no duplicate qty flag, no duplicate nag push")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_resting_stop_warning_fires_on_broker_close_after_handoff():
    try:
        s = _store()
        t = _trade(s)
        s.note_stop_handoff(t.id, 100.40)
        seqs = [_pos(75)] + [_pos(0, sell_price=141.0)] * _FLAT_CONFIRMS
        svc = _svc(s, seqs)
        pushes = []
        svc.notify = lambda title, body: pushes.append((title, body))
        for _ in range(_FLAT_CONFIRMS + 1):
            svc.reconcile_once()
        row = s.get(t.id)
        warn = [e for e in row.events if e.kind == "resting_stop_warning"]
        assert warn and "CANCEL IT" in warn[0].note, [e.kind for e in row.events]
        assert any("CANCEL RESTING STOP" in p[0] for p in pushes), pushes
        print("  INTEG  -> broker close after SL hand-off: journaled AND phoned")
    finally:
        trade_service_mod.reconcile.fetch_broker_positions = _REAL_FETCH


def test_invalidation_is_sticky_until_acknowledged():
    s = _store()
    t = _trade(s)
    # Spot breaks the level (CE invalidates when spot < level).
    monitor.evaluate(t, 118.0, 24250.0, 600)
    assert t.recommendation is TradeAction.INVALIDATED
    assert t.invalidation_fired_at is not None
    # Spot recovers — the advice must NOT quietly return to hold.
    monitor.evaluate(t, 121.0, 24360.0, 600)
    assert t.recommendation is TradeAction.INVALIDATED, t.recommendation
    assert "UNACKNOWLEDGED" in t.recommendation_note
    assert "spot back inside" in t.recommendation_note
    # Acknowledged: normal management resumes. (Backdate the fire so the ack
    # is STRICTLY newer — a same-second tie deliberately counts as unacked.)
    t.invalidation_fired_at -= 5
    with s._lock:
        s._trades[t.id] = t
        s._save()
    assert s.ack_invalidation(t.id) is not None
    acked = s.get(t.id)
    monitor.evaluate(acked, 121.0, 24360.0, 600)
    assert acked.recommendation is TradeAction.HOLD, acked.recommendation
    # A LATER re-break re-latches a fresh episode (>= : tie still nags).
    monitor.evaluate(acked, 118.0, 24250.0, 600)
    assert acked.recommendation is TradeAction.INVALIDATED
    assert acked.invalidation_fired_at >= acked.invalidation_ack_at
    print("  INTEG  -> invalidation sticks through recovery; ack releases; re-break re-latches")


def test_sticky_invalidation_never_masks_real_exits():
    s = _store()
    t = _trade(s)
    monitor.evaluate(t, 118.0, 24250.0, 600)      # fired, unacked
    monitor.evaluate(t, 99.0, 24360.0, 600)       # stop hit while unacked
    assert t.recommendation is TradeAction.STOPLOSS, t.recommendation
    print("  INTEG  -> a stop hit still outranks the sticky invalidation")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
