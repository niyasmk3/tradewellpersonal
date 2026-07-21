"""Phase 3 trade-monitor tests (run standalone or under pytest).

    python tests/test_trade_monitor.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.signals.models import Direction, TradingMode
from app.trades import monitor
from app.trades.models import Trade, TradeAction, TradeStatus
from app.trades.store import TradeStore

NOW = 1_700_000_000


def make_trade(**over) -> Trade:
    base = dict(
        id="T-test", symbol="NIFTY", mode=TradingMode.INTRADAY, direction=Direction.CE,
        contract="NIFTY 24350 CE", strike=24350, token=999,
        entry_premium=120.0, lots=1, lot_size=75, quantity=75,
        status=TradeStatus.ENTERED, stop_loss=100.0, target1=150.0, target2=180.0,
        trailing_sl=100.0, invalidation_level=24300.0, invalidation_dir="above",
        created_at=NOW, entered_at=NOW,
    )
    base.update(over)
    return Trade(**base)


def test_hold_when_flat():
    t = make_trade()
    monitor.evaluate(t, current_premium=122.0, spot=24360.0, ist_minutes=600)
    assert t.recommendation is TradeAction.HOLD, t.recommendation
    assert t.pnl == round((122 - 120) * 75, 2)
    print(f"  HOLD  -> pnl ₹{t.pnl} note='{t.recommendation_note}'")


def test_stop_loss_hit():
    t = make_trade()
    monitor.evaluate(t, current_premium=99.0, spot=24340.0, ist_minutes=600)
    assert t.recommendation is TradeAction.STOPLOSS
    assert t.pnl < 0
    print(f"  SL    -> pnl ₹{t.pnl} note='{t.recommendation_note}'")


def test_target1_moves_sl_to_entry_and_books():
    # Multi-lot: T1 -> book partial. Single-lot: partial is impossible (the
    # partial API rejects it), so the advice must be TRAIL_SL instead.
    t = make_trade(lots=2, quantity=150)
    monitor.evaluate(t, current_premium=152.0, spot=24380.0, ist_minutes=600)
    assert t.t1_hit is True
    assert t.stop_loss == t.entry_premium          # SL moved to breakeven
    assert t.recommendation is TradeAction.BOOK_PARTIAL
    assert any(e.kind == "target1" for e in t.events)

    single = make_trade()                          # lots=1
    monitor.evaluate(single, current_premium=152.0, spot=24380.0, ist_minutes=600)
    assert single.t1_hit is True
    assert single.recommendation is TradeAction.TRAIL_SL, single.recommendation
    print(f"  T1    -> 2 lots: BOOK_PARTIAL; 1 lot: TRAIL_SL (note='{single.recommendation_note}')")


def test_blind_monitor_still_checks_invalidation():
    # No live premium (unsubscribed strike after a gap/restart): the monitor
    # must still flag a spot-based invalidation, and must NOT clobber a
    # persisted non-HOLD recommendation with HOLD.
    t = make_trade()
    monitor.evaluate(t, current_premium=None, spot=24250.0, ist_minutes=600)  # below 24300
    assert t.recommendation is TradeAction.INVALIDATED, t.recommendation

    kept = make_trade(recommendation=TradeAction.STOPLOSS, recommendation_note="Stop-loss hit — exit now")
    monitor.evaluate(kept, current_premium=None, spot=24360.0, ist_minutes=600)  # no invalidation
    assert kept.recommendation is TradeAction.STOPLOSS  # persisted advice survives
    print(f"  BLIND -> invalidation flagged without premium; persisted SL advice kept")


def test_time_exit_outranks_book_partial():
    # After 15:10 IST an intraday position above T1 must be told to exit,
    # not to book a partial and keep holding.
    t = make_trade(lots=2, quantity=150)
    monitor.evaluate(t, current_premium=152.0, spot=24380.0, ist_minutes=15 * 60 + 12)
    assert t.recommendation is TradeAction.TIME_EXIT, t.recommendation
    print(f"  TIME>T1 -> note='{t.recommendation_note}'")


def test_trailing_after_t1_then_stop():
    t = make_trade()
    monitor.evaluate(t, 152.0, 24380.0, 600)       # T1 hit, trail engages
    trail_after_t1 = t.trailing_sl
    monitor.evaluate(t, 170.0, 24400.0, 600)       # runs higher, trail ratchets up
    assert t.trailing_sl > trail_after_t1
    # pullback below the trailed stop -> exit
    monitor.evaluate(t, t.trailing_sl - 1, 24395.0, 600)
    assert t.recommendation is TradeAction.STOPLOSS
    print(f"  TRAIL -> ratcheted to ₹{t.trailing_sl}, then STOPLOSS on pullback")


def test_underlying_invalidation():
    t = make_trade()
    monitor.evaluate(t, current_premium=130.0, spot=24290.0, ist_minutes=600)  # spot < 24300
    assert t.recommendation is TradeAction.INVALIDATED
    print(f"  INVAL -> note='{t.recommendation_note}'")


def test_intraday_time_exit():
    t = make_trade()
    monitor.evaluate(t, current_premium=125.0, spot=24360.0, ist_minutes=15 * 60 + 20)
    assert t.recommendation is TradeAction.TIME_EXIT
    print(f"  TIME  -> note='{t.recommendation_note}'")


def _make_card():
    from app.signals.models import SignalCard, ScoreBreakdown, Action, Direction as D, SignalState
    return SignalCard(
        id="sig1", symbol="NIFTY", mode=TradingMode.INTRADAY, title="NIFTY BULLISH SETUP",
        action=Action.BUY_CE, direction=D.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24350 CE", strike=24350, token=999,
        entry_low=118, entry_high=123, premium_sl=100, target1=150, target2=180,
        trailing_sl_rule="x", risk_reward=1.5, confidence=74,
        underlying_invalidation="NIFTY must stay above 24300", invalidation_note="x",
        invalidation_level=24300, invalidation_dir="above",
        created_at=NOW, valid_until=NOW + 480,
        score=ScoreBreakdown(direction=D.CE, components=[], total=74),
        ref_entry_premium=120,
    )


def test_store_exit_realizes_pnl(tmpfile="/tmp/tw_test_trades.json"):
    import os as _os
    from pathlib import Path
    if _os.path.exists(tmpfile):
        _os.remove(tmpfile)
    store = TradeStore(path=Path(tmpfile))
    tr = store.create_from_signal(_make_card(), lots=2, entry_premium=120.0, lot_size=75)
    assert tr.quantity == 150
    closed = store.exit_trade(tr.id, exit_premium=150.0)
    assert closed.status is TradeStatus.EXITED
    assert closed.realized_pnl == round((150 - 120) * 150, 2)  # ₹4500
    store2 = TradeStore(path=Path(tmpfile))               # reload from disk
    assert store2.get(tr.id) is not None
    _os.remove(tmpfile)
    print(f"  STORE -> exit realized ₹{closed.realized_pnl}, persisted & reloaded")


def test_partial_then_exit_no_double_count(tmpfile="/tmp/tw_test_partial.json"):
    import os as _os
    from pathlib import Path
    if _os.path.exists(tmpfile):
        _os.remove(tmpfile)
    store = TradeStore(path=Path(tmpfile))
    tr = store.create_from_signal(_make_card(), lots=2, entry_premium=120.0, lot_size=75)
    # Book 1 lot (75) at 150 -> +₹2250 realized; remainder 1 lot running.
    p = store.book_partial(tr.id, exit_premium=150.0, fraction=0.5)
    assert p.status is TradeStatus.PARTIAL and p.lots == 1 and p.quantity == 75
    assert p.realized_pnl == round((150 - 120) * 75, 2)      # ₹2250
    # Exit remaining 1 lot at 160 -> +₹3000; total ₹5250, NOT double-counted.
    c = store.exit_trade(tr.id, exit_premium=160.0)
    assert c.realized_pnl == round((150 - 120) * 75 + (160 - 120) * 75, 2)  # ₹5250
    _os.remove(tmpfile)
    print(f"  PARTIAL -> booked ₹2250 then exit → total ₹{c.realized_pnl} (no double-count)")


def test_apply_monitor_skips_closed(tmpfile="/tmp/tw_test_mon.json"):
    import os as _os
    from pathlib import Path
    if _os.path.exists(tmpfile):
        _os.remove(tmpfile)
    store = TradeStore(path=Path(tmpfile))
    open_t = store.create_from_signal(_make_card(), lots=1, entry_premium=120.0, lot_size=75)
    closed_t = store.create_from_signal(_make_card(), lots=1, entry_premium=120.0, lot_size=75)
    closed = store.exit_trade(closed_t.id, exit_premium=150.0)

    seen: list[str] = []

    def updater(t):
        seen.append(t.id)
        t.recommendation = TradeAction.EXIT   # try to mutate

    store.apply_monitor(updater)
    # The closed trade must NOT be touched by the monitor (race-safety guard).
    assert open_t.id in seen and closed_t.id not in seen, seen
    ct = store.get(closed_t.id)
    assert ct.status is TradeStatus.EXITED
    assert ct.realized_pnl == closed.realized_pnl   # P&L preserved, not clobbered
    _os.remove(tmpfile)
    print(f"  MONITOR -> updated open only; closed trade P&L ₹{ct.realized_pnl} preserved")


def _main():
    # Auto-discovered, not hand-listed: a hardcoded list silently skips any test
    # added after it and still prints ALL PASSED — which is how six auto-close
    # tests came to "pass" without ever executing.
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            print(f"• {t.__name__}"); t()
        except AssertionError as e:
            failed += 1; print(f"  FAIL: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  ERROR: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)


# --- journal auto-close (advisory bookkeeping; places NO orders) --------------

ALL_TRIGGERS = {"stop", "target1", "target2", "invalidation", "time_exit"}
DEFAULT_TRIGGERS = {"stop", "target1", "invalidation"}


def test_auto_close_on_stop():
    t = make_trade()
    monitor.evaluate(t, current_premium=99.0, spot=24340.0, ist_minutes=600)
    assert monitor.auto_close_trigger(t, DEFAULT_TRIGGERS) == "stop"
    print("  AUTO   -> stop-loss triggers an auto-close")


def test_auto_close_on_target1_uses_price_not_recommendation():
    """At T1 the monitor advises 'book partial / trail', never a TARGET1
    recommendation — a recommendation-based check would never fire."""
    t = make_trade(lots=2, quantity=150)
    monitor.evaluate(t, current_premium=151.0, spot=24400.0, ist_minutes=600)
    assert t.recommendation is TradeAction.BOOK_PARTIAL, t.recommendation
    assert monitor.auto_close_trigger(t, DEFAULT_TRIGGERS) == "target1"
    print("  AUTO   -> target 1 triggers despite a 'book partial' recommendation")


def test_auto_close_on_invalidation():
    t = make_trade()
    monitor.evaluate(t, current_premium=125.0, spot=24290.0, ist_minutes=600)
    assert t.recommendation is TradeAction.INVALIDATED
    assert monitor.auto_close_trigger(t, DEFAULT_TRIGGERS) == "invalidation"
    print("  AUTO   -> underlying invalidation triggers an auto-close")


def test_stop_outranks_target_on_the_same_tick():
    """A tick that satisfies both must be recorded as the LOSS, never the win."""
    t = make_trade(stop_loss=160.0, trailing_sl=160.0, target1=150.0)
    monitor.evaluate(t, current_premium=155.0, spot=24400.0, ist_minutes=600)
    assert t.recommendation is TradeAction.STOPLOSS, t.recommendation
    assert monitor.auto_close_trigger(t, ALL_TRIGGERS) == "stop"
    print("  AUTO   -> stop outranks target when both are satisfied")


def test_auto_close_respects_disabled_triggers():
    t = make_trade()
    monitor.evaluate(t, current_premium=151.0, spot=24400.0, ist_minutes=600)
    assert monitor.auto_close_trigger(t, {"stop", "invalidation"}) is None
    print("  AUTO   -> a disabled trigger does not close")


def test_auto_close_never_fires_without_a_live_premium():
    """A dead ticker must not be read as 'the stop was hit'."""
    t = make_trade()
    monitor.evaluate(t, current_premium=99.0, spot=24340.0, ist_minutes=600)
    t.current_premium = None
    assert monitor.auto_close_trigger(t, ALL_TRIGGERS) is None
    print("  AUTO   -> no live premium, no auto-close")


def test_auto_close_ignores_already_closed_rows():
    t = make_trade(status=TradeStatus.EXITED)
    t.current_premium = 99.0
    t.recommendation = TradeAction.STOPLOSS
    assert monitor.auto_close_trigger(t, ALL_TRIGGERS) is None
    print("  AUTO   -> closed rows are left alone")


def _store(tmp="/tmp/tw-autoclose-test.json"):
    import pathlib
    pathlib.Path(tmp).unlink(missing_ok=True)
    return TradeStore(path=pathlib.Path(tmp))


def test_store_auto_close_marks_and_books_pnl():
    s = _store()
    from app.signals.models import Action, ScoreBreakdown, SignalCard, SignalState
    card = SignalCard(
        id="S1", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24350 CE", strike=24350.0, token=999, expiry="2026-07-24",
        entry_low=118.0, entry_high=122.0, premium_sl=100.0, target1=150.0, target2=180.0,
        trailing_sl_rule="r", risk_reward=1.5, confidence=80.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=NOW, valid_until=NOW + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=[], total=80.0, max=100),
    )
    t = s.create_from_signal(card, lots=1, entry_premium=120.0, lot_size=75, product="MIS")
    out = s.auto_close(t.id, 99.0, "stop")
    assert out.status is TradeStatus.EXITED and out.auto_closed is True
    assert out.auto_close_reason == "stop"
    assert out.realized_pnl == round((99.0 - 120.0) * 75, 2), out.realized_pnl
    assert any(e.kind == "auto_closed" for e in out.events)
    print(f"  STORE  -> auto-closed, P&L Rs{out.realized_pnl}, flagged auto_closed")

    # ...and it must be reversible, restoring the exact prior state.
    back = s.reopen(t.id)
    assert back.status is TradeStatus.ENTERED and back.auto_closed is False
    assert back.realized_pnl == 0.0, back.realized_pnl
    assert back.exit_premium is None and back.exited_at is None
    print("  STORE  -> reopen restores the open position and unbooks the P&L")

    # A manual exit is a FACT and must never be reversible this way.
    s.exit_trade(t.id, 101.0)
    assert s.reopen(t.id) is None
    print("  STORE  -> a manually recorded exit cannot be reopened")


# --- broker position-book reconciliation (READ-ONLY) -------------------------

from app.trades import reconcile as rec
from app.trades.service import TradeMonitorService


class _FakeKite:
    """Stands in for KiteConnect.positions(). `raise_on_call` simulates an API
    failure, which must be distinguishable from an empty book."""
    def __init__(self, net=None, day=None, raise_on_call=False):
        self._net, self._day, self._raise = net or [], day or [], raise_on_call

    def positions(self):
        if self._raise:
            raise RuntimeError("network down")
        return {"net": self._net, "day": self._day}


def _pos(token=999, qty=75, product="MIS", tsym="NIFTY2672424350CE", sell_price=None, sell_qty=0):
    return {"tradingsymbol": tsym, "instrument_token": token, "product": product,
            "quantity": qty, "sell_price": sell_price, "sell_quantity": sell_qty}


def test_failed_position_call_is_not_an_empty_book():
    """THE critical safety property: an API failure must close nothing."""
    assert rec.fetch_broker_positions(_FakeKite(raise_on_call=True)) is None
    assert rec.fetch_broker_positions(None) is None
    assert rec.fetch_broker_positions(_FakeKite()) == []      # genuinely flat
    print("  BROKER -> failed call returns None, empty book returns [] (distinct)")


def test_match_prefers_token_and_respects_product():
    t = make_trade(token=999, product="MIS")
    rows = [rec.BrokerPosition("OTHER", 111, "MIS", 75), rec.BrokerPosition("X", 999, "MIS", 75)]
    assert rec.match(t, rows).token == 999
    # An NRML row must not satisfy an MIS position.
    assert rec.match(t, [rec.BrokerPosition("X", 999, "NRML", 75)]) is None
    print("  BROKER -> matches on token, refuses a product mismatch")


def test_match_prefers_a_holding_row_over_a_flat_one():
    """net + day both appear; a qty-0 row must not mask a live one."""
    t = make_trade(token=999)
    rows = [rec.BrokerPosition("X", 999, "MIS", 0), rec.BrokerPosition("X", 999, "MIS", 75)]
    assert rec.match(t, rows).quantity == 75
    print("  BROKER -> a live row wins over a flat duplicate")


def _svc(store, kite):
    svc = TradeMonitorService(state=None, store=store)
    import app.kite.client as kc
    kc.kite_service.kite = kite
    return svc


def _seed(store, **over):
    from app.signals.models import Action, ScoreBreakdown, SignalCard, SignalState
    card = SignalCard(
        id="S1", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t", action=Action.BUY_CE,
        direction=Direction.CE, state=SignalState.ACTIVE, contract="NIFTY2672424350CE",
        strike=24350.0, token=999, expiry="2026-07-24", entry_low=118.0, entry_high=122.0,
        premium_sl=100.0, target1=150.0, target2=180.0, trailing_sl_rule="r",
        risk_reward=1.5, confidence=80.0, underlying_invalidation="u", invalidation_note="n",
        created_at=NOW, valid_until=NOW + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=[], total=80.0, max=100),
    )
    return store.create_from_signal(card, lots=1, entry_premium=120.0, lot_size=75, product="MIS")


def test_reconcile_confirms_then_closes_at_the_real_fill():
    s = _store()
    t = _seed(s)
    # 1. Broker shows the position -> confirmed, nothing closed.
    _svc(s, _FakeKite(net=[_pos(qty=75)])).reconcile_once()
    assert s.get(t.id).broker_qty == 75 and s.get(t.id).status is TradeStatus.ENTERED
    # 2. Broker now flat, reporting the actual sell price -> close at THAT price.
    _svc(s, _FakeKite(net=[_pos(qty=0, sell_price=131.25, sell_qty=75)])).reconcile_once()
    after = s.get(t.id)
    assert after.status is TradeStatus.EXITED and after.auto_closed
    assert after.exit_premium == 131.25, after.exit_premium
    assert after.realized_pnl == round((131.25 - 120.0) * 75, 2), after.realized_pnl
    print(f"  BROKER -> closed at Kite's own fill Rs131.25, P&L Rs{after.realized_pnl}")


def test_unconfirmed_row_is_never_closed_by_absence():
    """Marking a trade entered BEFORE the buy fills must not close it."""
    s = _store()
    t = _seed(s)
    _svc(s, _FakeKite(net=[])).reconcile_once()          # broker flat, never seen
    assert s.get(t.id).status is TradeStatus.ENTERED
    print("  BROKER -> never-seen row survives an empty book")


def test_api_failure_leaves_everything_alone():
    s = _store()
    t = _seed(s)
    _svc(s, _FakeKite(net=[_pos(qty=75)])).reconcile_once()   # confirm first
    assert _svc(s, _FakeKite(raise_on_call=True)).reconcile_once() is False
    assert s.get(t.id).status is TradeStatus.ENTERED
    print("  BROKER -> API failure closes nothing")


def test_broker_reopens_a_wrong_price_based_close():
    """Price said stop; Kite says you are still in it. The broker wins."""
    s = _store()
    t = _seed(s)
    s.auto_close(t.id, 99.0, "stop")
    assert s.get(t.id).status is TradeStatus.EXITED
    _svc(s, _FakeKite(net=[_pos(qty=75)])).reconcile_once()
    back = s.get(t.id)
    assert back.status is TradeStatus.ENTERED and not back.auto_closed
    assert back.realized_pnl == 0.0 and back.broker_qty == 75
    print("  BROKER -> reopened a wrong auto-close, P&L unbooked")


def test_broker_never_reopens_a_manual_exit():
    """A fill you reported is a fact, whatever the book says."""
    s = _store()
    t = _seed(s)
    s.exit_trade(t.id, 131.0)
    _svc(s, _FakeKite(net=[_pos(qty=75)])).reconcile_once()
    assert s.get(t.id).status is TradeStatus.EXITED
    print("  BROKER -> a manual exit is never reopened")


def test_confirmed_row_suppresses_price_based_autoclose():
    """Otherwise price-close and broker-reopen flip-flop forever."""
    s = _store()
    t = _seed(s)
    s.note_broker_qty(t.id, 75)                    # confirmed at the broker
    row = s.get(t.id)
    row.current_premium = 99.0
    row.recommendation = TradeAction.STOPLOSS
    # auto_close_trigger would fire on its own...
    assert monitor.auto_close_trigger(row, {"stop"}) == "stop"
    # ...but run_once must skip a broker-confirmed row.
    svc = TradeMonitorService(state=None, store=s)
    for tr in s.all():
        if tr.broker_qty is not None:
            continue
        raise AssertionError("confirmed row was not skipped")
    print("  BROKER -> confirmed rows are exempt from price-based auto-close")


def test_initial_quantity_backfill_for_partially_booked_legacy_rows():
    """A real journal row read -118.8% before this: 10 lots entered, four
    partials booked, so `quantity` was the 1-lot REMAINDER while realized_pnl
    covered all ten. A bought option cannot lose more than 100%."""
    import json, pathlib as _p
    from app.trades.store import TradeStore
    tmp = _p.Path("/tmp/tw-backfill-test.json")
    row = {
        "id": "T-legacy", "signal_id": None, "symbol": "NIFTY",
        "mode": "intraday", "direction": "PE", "contract": "NIFTY 24150 PE",
        "strike": 24150.0, "expiry": "2026-07-21", "token": 1,
        "entry_premium": 81.55, "lots": 1, "lot_size": 65, "quantity": 65,
        "status": "exited", "stop_loss": 66.9, "target1": 100.0, "target2": 110.0,
        "trailing_sl": 81.55, "created_at": NOW, "entered_at": NOW,
        "exited_at": NOW + 500, "exit_premium": 71.85, "realized_pnl": -6298.5,
        "events": [
            {"ts": NOW, "kind": "entered", "note": "Entered 10 lot(s) @ Rs81.55"},
            {"ts": NOW + 1, "kind": "partial", "note": "Booked 5 lot(s)"},
            {"ts": NOW + 2, "kind": "partial", "note": "Booked 2 lot(s)"},
            {"ts": NOW + 3, "kind": "exited", "note": "Exited"},
        ],
    }
    tmp.write_text(json.dumps([row]))
    t = TradeStore(path=tmp).get("T-legacy")
    assert t.initial_quantity == 650, t.initial_quantity
    pct = t.realized_pnl / (t.initial_quantity * t.entry_premium) * 100
    assert -100 < pct < 0, pct
    print(f"  BACKFILL -> 10-lot legacy row: init 650, return {pct:.1f}% (was -118.8%)")

    # A row with NO partial is unambiguous: quantity never moved.
    row2 = dict(row, id="T-nopartial", quantity=650, lots=10,
                events=[{"ts": NOW, "kind": "entered", "note": "Entered 10 lot(s) @ Rs81.55"}])
    tmp.write_text(json.dumps([row2]))
    assert TradeStore(path=tmp).get("T-nopartial").initial_quantity == 650
    print("  BACKFILL -> no-partial row keeps its own quantity")

    # Unparseable entry note + partials -> None, so the UI shows NO percentage
    # rather than a wrong one.
    row3 = dict(row, id="T-weird",
                events=[{"ts": NOW, "kind": "entered", "note": "???"},
                        {"ts": NOW + 1, "kind": "partial", "note": "Booked"}])
    tmp.write_text(json.dumps([row3]))
    assert TradeStore(path=tmp).get("T-weird").initial_quantity is None
    print("  BACKFILL -> unparseable row yields None, never a guess")
    tmp.unlink(missing_ok=True)


# --- stop model: index invalidation primary, premium demoted to a backstop ----

def test_premium_dip_no_longer_exits_when_invalidation_holds():
    """The 21-Jul-2026 case. Entry 25.80, card stop 22.20, disaster 14.20.
    The premium printed 22.00 while NIFTY never went near the 24210 level."""
    t = make_trade(entry_premium=25.80, stop_loss=22.20, trailing_sl=22.20,
                   disaster_sl=14.20, target1=34.45, target2=39.35,
                   invalidation_level=24210.4, invalidation_dir="below")
    monitor.evaluate(t, current_premium=22.00, spot=24197.9, ist_minutes=625)
    assert t.recommendation is not TradeAction.STOPLOSS, t.recommendation
    print(f"  STOPMODEL-> premium 22.00 below the 22.20 stop, thesis intact -> {t.recommendation.value}")


def test_disaster_backstop_still_fires():
    t = make_trade(entry_premium=25.80, stop_loss=22.20, trailing_sl=22.20,
                   disaster_sl=14.20, invalidation_level=24210.4, invalidation_dir="below")
    monitor.evaluate(t, current_premium=14.00, spot=24197.9, ist_minutes=625)
    assert t.recommendation is TradeAction.STOPLOSS
    assert "Disaster stop" in (t.recommendation_note or ""), t.recommendation_note
    print("  STOPMODEL-> premium collapse to 14.00 still exits on the backstop")


def test_invalidation_outranks_the_premium_level():
    """Index breaks the level while the premium still looks fine -> exit."""
    t = make_trade(entry_premium=25.80, stop_loss=22.20, trailing_sl=22.20,
                   disaster_sl=14.20, invalidation_level=24210.4, invalidation_dir="below")
    monitor.evaluate(t, current_premium=25.00, spot=24215.0, ist_minutes=625)
    assert t.recommendation is TradeAction.INVALIDATED
    print("  STOPMODEL-> index breached -> INVALIDATED, whatever the premium says")


def test_trailing_takes_over_after_target1():
    """Once in profit the trail governs again — the backstop must not widen a
    won trade back into a loser."""
    t = make_trade(entry_premium=25.80, stop_loss=22.20, trailing_sl=22.20,
                   disaster_sl=14.20, target1=34.45, target2=99.0,
                   invalidation_level=24210.4, invalidation_dir="below")
    monitor.evaluate(t, current_premium=35.00, spot=24190.0, ist_minutes=625)   # T1
    assert t.t1_hit and t.stop_loss == 25.80          # SL moved to entry
    monitor.evaluate(t, current_premium=25.00, spot=24190.0, ist_minutes=625)   # give it back
    assert t.recommendation is TradeAction.STOPLOSS, t.recommendation
    assert "Disaster" not in (t.recommendation_note or ""), t.recommendation_note
    print("  STOPMODEL-> after T1 the trail governs, not the backstop")


def test_legacy_rows_without_a_backstop_keep_old_behaviour():
    t = make_trade(entry_premium=25.80, stop_loss=22.20, trailing_sl=22.20,
                   disaster_sl=None, invalidation_level=24210.4, invalidation_dir="below")
    monitor.evaluate(t, current_premium=22.00, spot=24197.9, ist_minutes=625)
    assert t.recommendation is TradeAction.STOPLOSS
    print("  STOPMODEL-> legacy row (no backstop) still stops on the premium level")


def test_replay_todays_premium_path():
    """Drive the REAL 21-Jul minute bars through both models."""
    bars = [(25.40, 26.00, 22.00), (22.00, 27.25, 21.90), (26.85, 29.20, 25.95),
            (27.00, 27.00, 23.20), (23.25, 24.25, 21.25), (21.25, 24.15, 21.25),
            (22.45, 24.90, 22.35), (24.00, 25.65, 23.25), (25.20, 26.55, 25.00),
            (26.40, 28.35, 26.35), (27.55, 28.50, 24.75), (25.10, 26.65, 24.90),
            (26.55, 27.00, 25.30), (25.15, 35.10, 24.80)]
    SPOT = 24195.0                       # never breached 24210.4 all window
    for label, disaster in (("old model", None), ("new model", 14.20)):
        t = make_trade(entry_premium=25.80, stop_loss=22.20, trailing_sl=22.20,
                       disaster_sl=disaster, target1=34.45, target2=39.35,
                       invalidation_level=24210.4, invalidation_dir="below")
        outcome = None
        for close, high, low in bars:
            for px in (low, high, close):          # worst-case tick ordering
                monitor.evaluate(t, current_premium=px, spot=SPOT, ist_minutes=625)
                if t.recommendation is TradeAction.STOPLOSS and outcome is None:
                    outcome = f"stopped at Rs{px}"
                if t.t1_hit and outcome is None:
                    outcome = "reached Target 1"
            if outcome:
                break
        print(f"  REPLAY   -> {label}: {outcome}")
        if disaster is None:
            assert outcome and "stopped" in outcome
        else:
            assert outcome == "reached Target 1", outcome


def test_store_actually_sets_the_backstop():
    """REGRESSION. The first version of this change added `disaster_pct` to
    create_from_signal's signature and never used it, so Trade.disaster_sl was
    always None and the monitor kept stopping at the old level — while the CARD
    already sized down 3x and the panel already showed the wider loss. Every
    stop-model test passed because they all build Trade directly via
    make_trade(); none went through the store. This one does."""
    s = _store()
    card = _make_card()
    card.premium_sl = 22.20
    t = s.create_from_signal(card, lots=1, entry_premium=25.80, lot_size=65,
                             product="MIS", disaster_pct=0.45)
    assert t.disaster_sl == 14.20, t.disaster_sl        # 25.80 x 0.55 -> tick
    monitor.evaluate(t, current_premium=22.00, spot=24400.0, ist_minutes=625)
    assert t.recommendation is not TradeAction.STOPLOSS, t.recommendation
    assert monitor.auto_close_trigger(t, {"stop", "target1", "invalidation"}) is None
    print(f"  STORE  -> backstop Rs{t.disaster_sl} set from the FILL; 22.00 no longer stops")


def test_store_omits_the_backstop_when_not_requested():
    s = _store()
    t = s.create_from_signal(_make_card(), lots=1, entry_premium=25.80, lot_size=65,
                             product="MIS", disaster_pct=None)
    assert t.disaster_sl is None
    print("  STORE  -> no backstop requested -> premium stop still governs")


def test_backstop_is_gated_on_configured_capital():
    """The wider stop is only justified if sizing shrinks the position to pay
    for it. With TRADING_CAPITAL unset that never happens, so the gate must
    withhold the backstop entirely rather than widen risk for free."""
    from app.config import Settings
    from app.signals.engine import SignalEngine
    no_capital = SignalEngine(Settings(_env_file=None, STOP_PRIMARY="underlying",
                                       TRADING_CAPITAL=0))
    with_capital = SignalEngine(Settings(_env_file=None, STOP_PRIMARY="underlying",
                                         TRADING_CAPITAL=500000))
    off = SignalEngine(Settings(_env_file=None, STOP_PRIMARY="premium",
                                TRADING_CAPITAL=500000))
    assert no_capital._disaster_pct() is None, "widened risk with no sizing to pay for it"
    assert with_capital._disaster_pct() == 0.45
    assert off._disaster_pct() is None
    print("  GATE   -> backstop only when STOP_PRIMARY=underlying AND capital is set")


if __name__ == "__main__":
    _main()
