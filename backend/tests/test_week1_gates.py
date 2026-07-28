"""Week-1 forensic gates: late-entry cutoff, re-fire guard, early de-risk,
and the exit-reason nudge/prompt widening.

Every rule here was priced by the 28-Jul weekly audit: 14:27+ cards lost
7-for-7; a stopped strike re-fired twice and lost both; ten fills peaked
+1.9..+11.3% and all closed negative with nothing protecting them below the
+12% quick target; and the exit-reason prompt sat 9-for-9 unanswered.

Run:  python backend/tests/test_week1_gates.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
import time
from pathlib import Path

import pandas as pd

from app.config import Settings
from app.signals.models import (
    Action,
    Bias,
    Direction,
    MarketStatus,
    Regime,
    ScoreBreakdown,
    SignalCard,
    SignalResponse,
    SignalState,
    TradingMode,
)
from app.signals.service import SignalService, _parse_hhmm
from app.trades import monitor
from app.trades.models import TradeStatus
from app.trades.store import TradeStore


def _cfg(**over):
    return Settings(_env_file=None, **over)


def _ist_epoch(hhmm: str) -> int:
    """Epoch for HH:MM IST today."""
    hh, mm = hhmm.split(":")
    day = (int(time.time()) + 19800) // 86400
    return day * 86400 - 19800 + int(hh) * 3600 + int(mm) * 60


def _card(cid="W", mode=TradingMode.INTRADAY, direction=Direction.PE,
          strike=24000.0, entry=100.0, at=None):
    at = at or int(time.time())
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=mode, title="t",
        action=Action.BUY_PE if direction is Direction.PE else Action.BUY_CE,
        direction=direction, state=SignalState.ACTIVE,
        contract=f"NIFTY {strike:.0f} {direction.value}", strike=strike,
        expiry="2026-07-30", entry_low=entry - 1, entry_high=entry + 1,
        premium_sl=round(entry * 0.82, 2), target1=round(entry * 1.27, 2),
        target2=round(entry * 1.45, 2), trailing_sl_rule="r", risk_reward=1.5,
        confidence=81.0, underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=direction, components=[], total=81.0),
    )


def _resp(mode, at):
    return SignalResponse(
        symbol="NIFTY", mode=mode, evaluated_at=at,
        status=MarketStatus(
            symbol="NIFTY", mode=mode, regime=Regime.MODERATE_BULLISH,
            regime_label="R", bias=Bias.BULLISH, bull_score=60, bear_score=40,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=Action.BUY_CE, signal=None, no_trade_reason=None, score=None,
    )


def _svc(**over):
    svc = SignalService.__new__(SignalService)
    svc.cfg = _cfg(**over)
    svc.state = object()          # no gap_ended_at -> post-gap veto inert
    return svc


def _df(now):
    return pd.DataFrame([{"ts": now - 180, "open": 1, "high": 1, "low": 1,
                          "close": 1, "volume": 1}])


def test_parse_hhmm():
    assert _parse_hhmm("14:15") == 855
    assert _parse_hhmm("") is None and _parse_hhmm("nonsense") is None
    assert _parse_hhmm("25:00") is None
    print("  HHMM   -> parses, and garbage disables instead of crashing")


def test_late_entry_cutoff():
    svc = _svc(SIGNAL_POST_GAP_QUIET_S=0)
    late, early = _ist_epoch("14:20"), _ist_epoch("13:50")
    # Intraday and scalp are refused at/after the cutoff, with the reason spoken.
    for mode in (TradingMode.INTRADAY, TradingMode.SCALP):
        v = svc._context_veto(_resp(mode, late), _df(late), late)
        assert v and "entry cutoff" in v, (mode, v)
        assert svc._context_veto(_resp(mode, early), _df(early), early) is None
    # Positional carries overnight — exempt.
    assert svc._context_veto(_resp(TradingMode.POSITIONAL, late), _df(late), late) is None
    # Empty setting disables the gate entirely.
    off = _svc(SIGNAL_POST_GAP_QUIET_S=0, SIGNAL_ENTRY_CUTOFF_IST="")
    assert off._context_veto(_resp(TradingMode.INTRADAY, late), _df(late), late) is None
    print("  CUTOFF -> 14:15+ intraday/scalp refused; positional exempt; empty=off")


def test_refire_guard():
    import app.trades.store as ts_mod
    from app.services import feed

    with tempfile.TemporaryDirectory() as d:
        paper = TradeStore(path=Path(d) / "p.json")
        empty_live = TradeStore(path=Path(d) / "l.json")
        now = int(time.time())
        # A 24000 PE paper fill stopped out 30 minutes ago.
        t = paper.create_from_signal(_card("S1", strike=24000.0), 1, 100.0, 65)
        paper.auto_close(t.id, 82.0, "stop")
        row = paper._trades[t.id]
        row.exited_at = now - 1800

        orig_live, orig_paper = ts_mod.trade_store, getattr(feed, "paper_store", None)
        ts_mod.trade_store = empty_live
        feed.paper_store = paper
        try:
            svc = _svc()
            # Same strike + direction: blocked, reason names the stopped fill.
            v = svc._refire_veto(_card("N1", strike=24000.0), now)
            assert v and "Re-fire guard" in v, v
            # Adjacent strike (1-2 steps) still the same thesis: blocked.
            assert svc._refire_veto(_card("N2", strike=24100.0), now) is not None
            # Far strike, opposite direction, or outside the window: allowed.
            assert svc._refire_veto(_card("N3", strike=24300.0), now) is None
            assert svc._refire_veto(
                _card("N4", strike=24000.0, direction=Direction.CE), now) is None
            assert svc._refire_veto(_card("N5", strike=24000.0), now + 7200) is None
            # Hollow counterfactual stops must NOT arm the guard.
            h = paper.create_from_signal(
                _card("H1", strike=23800.0), 1, 100.0, 65,
                notes="hollow: vol floor")
            paper.auto_close(h.id, 82.0, "stop")
            paper._trades[h.id].exited_at = now - 600
            assert svc._refire_veto(_card("N6", strike=23800.0), now) is None
            # A LOSING broker-flat live close arms it too: a real live stop
            # is recorded as "broker flat" by the reconciler, never "stop".
            lb = empty_live.create_from_signal(_card("LB", strike=23600.0), 1, 100.0, 65)
            empty_live.auto_close(lb.id, 90.0, "broker flat", price_source="broker")
            empty_live._trades[lb.id].exited_at = now - 900
            svc2 = _svc()
            assert svc2._refire_veto(_card("N8", strike=23600.0), now) is not None
            # ...but a WINNING broker-flat (target/profit take) must not.
            wb = empty_live.create_from_signal(_card("WB", strike=23400.0), 1, 100.0, 65)
            empty_live.auto_close(wb.id, 110.0, "broker flat", price_source="broker")
            empty_live._trades[wb.id].exited_at = now - 900
            svc3 = _svc()
            assert svc3._refire_veto(_card("N9", strike=23400.0), now) is None
            # 0 disables.
            assert _svc(SIGNAL_REFIRE_GUARD_S=0)._refire_veto(
                _card("N7", strike=24000.0), now) is None
        finally:
            ts_mod.trade_store = orig_live
            feed.paper_store = orig_paper
    print("  REFIRE -> stopped thesis blocked (adjacent too); CE/far/late/hollow pass")


def test_early_derisk_moves_stop_to_entry():
    with tempfile.TemporaryDirectory() as d:
        s = TradeStore(path=Path(d) / "t.json")
        t = s.create_from_signal(_card("D1"), 1, 100.0, 65, quick_pct=0.12)
        # +4% is not enough at the default 5% trigger.
        monitor.evaluate(t, 104.0, None, 690, "2026-07-28", 0, early_derisk_pct=0.05)
        assert t.stop_loss < 100.0 and not t.t0_hit
        # +6% MFE: stop to entry, event logged, disaster gate retired.
        monitor.evaluate(t, 106.0, None, 690, "2026-07-28", 0, early_derisk_pct=0.05)
        assert t.stop_loss == 100.0, t.stop_loss
        assert t.trailing_sl >= 100.0
        assert any(e.kind == "early_derisk" for e in t.events)
        # A pullback to entry now reads STOPLOSS instead of riding to -18%.
        monitor.evaluate(t, 99.5, None, 690, "2026-07-28", 0, early_derisk_pct=0.05)
        assert t.recommendation.value == "stop_loss_hit", t.recommendation
        # Disabled: the stop stays where the ladder put it.
        t2 = s.create_from_signal(_card("D2"), 1, 100.0, 65, quick_pct=0.12)
        monitor.evaluate(t2, 106.0, None, 690, "2026-07-28", 0, early_derisk_pct=0)
        assert t2.stop_loss < 100.0
        # Positional keeps its own +5% rule regardless of this knob.
        p = _card("D3", mode=TradingMode.POSITIONAL)
        t3 = s.create_from_signal(p, 1, 100.0, 65)
        monitor.evaluate(t3, 106.0, None, 690, "2026-07-28", 0, early_derisk_pct=0)
        assert t3.stop_loss == 100.0, "positional breakeven must be untouched"
    print("  DERISK -> +5% MFE lifts stop to entry for intraday/scalp; 0=off")


def test_exit_reason_nudge_pushes_once():
    from app.trades.service import TradeMonitorService

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "t.json")
        t = store.create_from_signal(_card("R1"), 1, 100.0, 65)
        store.auto_close(t.id, 101.0, "broker flat", price_source="broker")
        store._trades[t.id].exited_at = int(time.time()) - 1200   # 20 min ago

        svc = TradeMonitorService.__new__(TradeMonitorService)
        svc.store = store
        pushes = []
        svc.notify = lambda title, body: pushes.append(title)
        svc._nudge_missing_exit_reasons()
        assert len(pushes) == 1 and "Why did you exit" in pushes[0], pushes
        # Once, ever: the reason_nudge event is the dedupe flag.
        svc._nudge_missing_exit_reasons()
        assert len(pushes) == 1
        # An answered row never nudges; a fresh close (<15m) waits.
        t2 = store.create_from_signal(_card("R2"), 1, 100.0, 65)
        store.auto_close(t2.id, 101.0, "broker flat")
        store._trades[t2.id].exited_at = int(time.time()) - 1200
        store.set_exit_reason(t2.id, "broker stop")
        t3 = store.create_from_signal(_card("R3"), 1, 100.0, 65)
        store.auto_close(t3.id, 101.0, "broker flat")
        # A PRE-FEATURE historic row (days old) must never burst-push: the
        # real journal held nine such rows at deploy time.
        t4 = store.create_from_signal(_card("R4"), 1, 100.0, 65)
        store.auto_close(t4.id, 101.0, "broker flat")
        store._trades[t4.id].exited_at = int(time.time()) - 3 * 86400
        svc._nudge_missing_exit_reasons()
        assert len(pushes) == 1, pushes
    print("  NUDGE  -> one push per stale unreasoned broker-flat close; no backlog burst")


def test_run_once_survives_an_open_trade():
    """THE review-confirmed critical: run_once()'s updater referenced
    self.cfg, which TradeMonitorService does not have — one open trade made
    every monitor cycle die with AttributeError, silently no-opping stops,
    invalidations, auto-closes and nudges. No test drove run_once() with an
    open row until this one."""
    from app.state import MarketState
    from app.trades.service import TradeMonitorService

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "t.json")
        t = store.create_from_signal(_card("O1"), 1, 100.0, 65, quick_pct=0.12)
        state = MarketState()
        state.ticks[999] = {"last_price": 104.0}
        svc = TradeMonitorService(state, store)
        svc.run_once()                      # must not raise
        assert store.get(t.id).status.value in ("entered", "partial")
    print("  LOOP   -> run_once with an open trade completes (self.cfg regression)")


def test_needs_reason_covers_manual_exits():
    from app.trades.analytics import summarize

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "t.json")
        m = store.create_from_signal(_card("M1"), 1, 100.0, 65)
        store.exit_trade(m.id, 90.0)                      # manual, no reason
        b = store.create_from_signal(_card("M2"), 1, 100.0, 65)
        store.auto_close(b.id, 101.0, "broker flat")      # broker flat, no reason
        a = store.create_from_signal(_card("M3"), 1, 100.0, 65)
        store.auto_close(a.id, 82.0, "stop")              # plan trigger: not askable
        r = store.create_from_signal(_card("M4"), 1, 100.0, 65)
        store.exit_trade(r.id, 105.0)
        store.set_exit_reason(r.id, "target hit")         # answered manual

        out = summarize(store.all())
        assert set(out["needs_reason"]) == {m.id, b.id}, out["needs_reason"]
    print("  ASKSET -> manual + broker-flat unreasoned rows listed; others not")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
