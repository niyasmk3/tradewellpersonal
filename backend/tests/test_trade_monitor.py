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


if __name__ == "__main__":
    _main()
