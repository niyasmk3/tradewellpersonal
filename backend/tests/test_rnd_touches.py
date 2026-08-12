"""R1 touch ladder: first-touch latching on the trade monitor.

The ladder answers window questions ("+5% within 30 min?") that mfe/mae
cannot — these tests pin the latch semantics: first observation wins, never
overwritten, both signs, absent premium records nothing.
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.signals.models import Direction, TradingMode
from app.trades import monitor
from app.trades.models import Trade, TradeStatus

NOW = int(time.time())


def mk(entry=100.0, **over):
    base = dict(
        id=f"T{id(over)}", symbol="NIFTY", mode=TradingMode.INTRADAY,
        direction=Direction.CE, contract="NIFTY 24350 CE", strike=24350,
        token=999, entry_premium=entry, lots=1, lot_size=75, quantity=75,
        initial_quantity=75, status=TradeStatus.ENTERED, stop_loss=82.0,
        target1=127.0, target2=145.0, trailing_sl=82.0,
        created_at=NOW, entered_at=NOW,
    )
    base.update(over)
    return Trade(**base)


def test_first_touch_latches_and_never_overwrites():
    t = mk(entry=100.0)
    monitor.evaluate(t, current_premium=104.0, spot=24400.0, ist_minutes=600)
    assert t.touch_times.get("+3") is not None
    first = t.touch_times["+3"]
    assert "+5" not in t.touch_times
    monitor.evaluate(t, current_premium=106.0, spot=24400.0, ist_minutes=600)
    assert t.touch_times["+3"] == first          # latched, not refreshed
    assert "+5" in t.touch_times
    # Retreat below the level, then re-cross: the ORIGINAL stamp survives.
    monitor.evaluate(t, current_premium=101.0, spot=24400.0, ist_minutes=600)
    monitor.evaluate(t, current_premium=107.0, spot=24400.0, ist_minutes=600)
    assert t.touch_times["+3"] == first
    assert list(t.touch_times) == ["+3", "+5"]


def test_gap_jump_stamps_all_crossed_levels_at_once():
    t = mk(entry=100.0)
    monitor.evaluate(t, current_premium=112.0, spot=24400.0, ist_minutes=600)
    assert set(t.touch_times) == {"+3", "+5", "+10"}
    assert len({t.touch_times[k] for k in t.touch_times}) == 1  # same observation


def test_negative_side_mirrors():
    t = mk(entry=100.0)
    monitor.evaluate(t, current_premium=94.0, spot=24400.0, ist_minutes=600)
    assert set(t.touch_times) == {"-3", "-5"}
    monitor.evaluate(t, current_premium=79.0, spot=24400.0, ist_minutes=600)
    assert "-20" in t.touch_times and "-10" in t.touch_times


def test_both_signs_can_coexist():
    """A whipsaw trade records both sides — the ladder is a path record, not
    a verdict, and adverse-before-favorable is exactly the cost-of-capture
    question R2 exists to answer."""
    t = mk(entry=100.0)
    monitor.evaluate(t, current_premium=96.5, spot=24400.0, ist_minutes=600)
    monitor.evaluate(t, current_premium=105.5, spot=24400.0, ist_minutes=600)
    assert "-3" in t.touch_times and "+5" in t.touch_times
    assert t.touch_times["-3"] <= t.touch_times["+5"]


def test_absent_or_zero_premium_records_nothing():
    t = mk(entry=100.0)
    monitor.evaluate(t, current_premium=None, spot=24400.0, ist_minutes=600)
    monitor.evaluate(t, current_premium=0.0, spot=24400.0, ist_minutes=600)
    assert t.touch_times == {}


def test_closed_trade_records_nothing():
    t = mk(entry=100.0, status=TradeStatus.EXITED, exited_at=NOW + 60)
    monitor.evaluate(t, current_premium=110.0, spot=24400.0, ist_minutes=600)
    assert t.touch_times == {}


def test_exact_level_counts_as_touched():
    t = mk(entry=100.0)
    monitor.evaluate(t, current_premium=105.0, spot=24400.0, ist_minutes=600)
    assert "+5" in t.touch_times


def test_touch_from_stamped_on_first_ladder_cycle():
    t = mk(entry=100.0)
    assert t.touch_from is None
    monitor.evaluate(t, current_premium=100.5, spot=24400.0, ist_minutes=600)
    assert t.touch_from is not None        # stamped even with no level crossed
    first = t.touch_from
    monitor.evaluate(t, current_premium=106.0, spot=24400.0, ist_minutes=600)
    assert t.touch_from == first           # never restamped


def test_exact_tick_aligned_level_not_lost_to_float(monkeypatch):
    """entry 91.35 -> +5% is 95.9175; a print at 95.92 must stamp +5 even
    when the percentage lands within float error of the level (review C3)."""
    t = mk(entry=91.35)
    monitor.evaluate(t, current_premium=95.9175, spot=24400.0, ist_minutes=600)
    assert "+5" in t.touch_times


def test_reversible_window_latches_and_reopen_folds():
    """Touches during the post-auto-close blind window survive a reopen with
    their REAL timestamps (review C2/C4)."""
    from app.trades.store import TradeStore
    t = mk(entry=100.0)
    monitor.evaluate(t, current_premium=101.0, spot=24400.0, ist_minutes=600)
    # Auto-close, then a +12% spike during the reversible window:
    t.status = TradeStatus.EXITED
    t.auto_closed = True
    t.exit_premium = 101.0
    t.realized_pnl = round((101.0 - 100.0) * t.quantity, 2)
    monitor.track_reversible(t, 112.0)
    assert set(t.post_close_touch_times) == {"+3", "+5", "+10"}
    assert t.touch_times == {}             # trade record untouched while closed
    spike_ts = t.post_close_touch_times["+5"]
    import tempfile
    from pathlib import Path
    st = TradeStore(path=Path(tempfile.mkdtemp()) / "p.json")   # house test pattern
    st._trades[t.id] = t
    reopened = st.reopen(t.id)
    assert reopened is not None and reopened.status == TradeStatus.ENTERED
    got = st._trades[t.id]
    assert got.touch_times["+5"] == spike_ts   # folded with the real time
    assert got.post_close_touch_times == {}


def test_survives_model_roundtrip():
    """Persistence path: dict field must serialize and reload intact."""
    t = mk(entry=100.0)
    monitor.evaluate(t, current_premium=106.0, spot=24400.0, ist_minutes=600)
    reloaded = Trade(**t.model_dump())
    assert reloaded.touch_times == t.touch_times


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  PASS {name}")
    print("all touch-ladder tests passed")
