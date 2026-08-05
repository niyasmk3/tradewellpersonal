"""WATCH->CONFIRM persistence gate (audit P2-3, sized 05-Aug).

The 60d replay: 58% of gate-crossings last one bar and lose -0.41R at 30%
WR (the 05-Aug 11:03 card's class); persistent episodes are the engine's
only positive class. These tests pin the state machine: runs build only on
CONSECUTIVE closed bars, reset on a gap or a sub-gate bar, same-bar
re-evals don't inflate, first-bar cards are vetoed WITH a confirm shadow,
confirmed cards pass, and the confounded rule books nowhere.

Run:  python backend/tests/test_confirm_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

import pandas as pd

from app.config import Settings
from app.signals.models import (
    Action,
    Bias,
    Direction,
    MarketStatus,
    Regime,
    ScoreBreakdown,
    ScoreComponent,
    SignalCard,
    SignalResponse,
    SignalState,
    TradingMode,
)
from app.signals.service import SignalService


def _svc(**over):
    svc = SignalService.__new__(SignalService)
    svc.cfg = Settings(_env_file=None, SIGNAL_POST_GAP_QUIET_S=0, **over)
    svc.state = object()
    return svc


def _profile(svc, mode=TradingMode.INTRADAY):
    from app.signals.modes import build_profiles

    return build_profiles(svc.cfg)[mode]


def _card(direction=Direction.CE, at=None, vol_pts=9.0, oi_pts=14.0):
    at = at or int(time.time())
    comps = [
        ScoreComponent(name="Volume confirmation", points=vol_pts, max=15,
                       reasons=["1.4x average volume"]),
        ScoreComponent(name="Options & OI", points=oi_pts, max=20),
    ]
    return SignalCard(
        id=f"C-{at}-{direction.value}", symbol="NIFTY", mode=TradingMode.INTRADAY,
        title="t", action=Action.BUY_CE if direction is Direction.CE else Action.BUY_PE,
        direction=direction, state=SignalState.ACTIVE,
        contract=f"NIFTY 24600 {direction.value}", strike=24600.0, expiry="2026-08-11",
        entry_low=99.0, entry_high=101.0, premium_sl=82.0, target1=127.0,
        target2=145.0, trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=direction, components=comps, total=81.0),
    )


def _fresh(bull, bear, card=None, mode=TradingMode.INTRADAY):
    return SignalResponse(
        symbol="NIFTY", mode=mode, evaluated_at=int(time.time()),
        status=MarketStatus(
            symbol="NIFTY", mode=mode, regime=Regime.STRONG_BULLISH,
            regime_label="R", bias=Bias.BULLISH, bull_score=bull, bear_score=bear,
            headline="h", vix_status=None, news_label=None, news_net=None,
            notes=[]),
        action=card.action if card else Action.WAIT, signal=card,
        no_trade_reason=None, score=None)


def _df(bar_ts):
    return pd.DataFrame([{"ts": bar_ts, "open": 1, "high": 1, "low": 1,
                          "close": 24600.0, "volume": 10}])


def _mid_morning():
    day = (int(time.time()) + 19800) // 86400
    return day * 86400 - 19800 + 11 * 3600


def test_first_bar_watches_second_bar_confirms():
    svc = _svc()
    prof = _profile(svc)
    t0 = _mid_morning()
    # Bar 1 closes above the gate (bull 80 >= 78): card is a WATCH.
    svc._track_gate_runs("NIFTY", prof, _df(t0), _fresh(80, 30), t0 + 180)
    v = svc._confirm_veto(_fresh(80, 30, _card()))
    assert v and "WATCH" in v and "needs 2" in v
    # Same bar re-evaluated 5s later: still a WATCH, run not inflated.
    svc._track_gate_runs("NIFTY", prof, _df(t0), _fresh(80, 30), t0 + 185)
    assert svc._gate_runs[("NIFTY", "intraday")]["CE"] == 1
    # Bar 2 closes above the gate: CONFIRMED, card passes.
    svc._track_gate_runs("NIFTY", prof, _df(t0 + 180), _fresh(79, 30), t0 + 360)
    assert svc._confirm_veto(_fresh(79, 30, _card())) is None
    print("  WATCH  -> first gate bar held back; second consecutive bar passes")


def test_run_resets_on_sub_gate_bar_and_gap():
    svc = _svc()
    prof = _profile(svc)
    t0 = _mid_morning()
    svc._track_gate_runs("NIFTY", prof, _df(t0), _fresh(80, 30), t0 + 180)
    # A sub-gate bar between crossings resets the run: 80, 65, 80 -> WATCH.
    svc._track_gate_runs("NIFTY", prof, _df(t0 + 180), _fresh(65, 30), t0 + 360)
    svc._track_gate_runs("NIFTY", prof, _df(t0 + 360), _fresh(80, 30), t0 + 540)
    v = svc._confirm_veto(_fresh(80, 30, _card()))
    assert v and "WATCH" in v, "the 11:03 pattern (78.3 for one bar) must WATCH"
    # A missing bar (feed hole) also breaks contiguity.
    svc._track_gate_runs("NIFTY", prof, _df(t0 + 900), _fresh(80, 30), t0 + 1080)
    assert svc._gate_runs[("NIFTY", "intraday")]["CE"] == 1
    print("  RESET  -> sub-gate bars and holes break the run")


def test_directions_confirm_independently():
    svc = _svc()
    prof = _profile(svc)
    t0 = _mid_morning()
    # Two bars of BEAR strength: PE confirmed, CE is still a first-bar WATCH.
    svc._track_gate_runs("NIFTY", prof, _df(t0), _fresh(30, 80), t0 + 180)
    svc._track_gate_runs("NIFTY", prof, _df(t0 + 180), _fresh(80, 80), t0 + 360)
    pe = _card(Direction.PE)
    assert svc._confirm_veto(_fresh(80, 80, pe)) is None
    ce = _card(Direction.CE)
    v = svc._confirm_veto(_fresh(80, 80, ce))
    assert v and "WATCH" in v
    print("  DIRS   -> CE and PE keep independent runs")


def test_scope_and_off_switch():
    svc = _svc()
    t0 = _mid_morning()
    # Positional is exempt (15m bars — flicker is not its failure mode).
    from app.signals.modes import build_profiles

    profs = build_profiles(svc.cfg)
    pos_card = _card()
    pos_card.mode = TradingMode.POSITIONAL
    fresh = _fresh(80, 30, pos_card, mode=TradingMode.POSITIONAL)
    assert svc._confirm_veto(fresh) is None
    # SIGNAL_CONFIRM_BARS=0 disables entirely.
    off = _svc(SIGNAL_CONFIRM_BARS=0)
    assert off._confirm_veto(_fresh(80, 30, _card())) is None
    print("  SCOPE  -> positional exempt; 0 disables")


def test_confirm_shadow_tag_and_confounding():
    """Confirm alone -> its own shadow class; confirm + floor -> vetoed with
    the floor's reason and booked NOWHERE (the purity rule)."""
    svc = _svc()
    prof = _profile(svc)
    t0 = _mid_morning()
    svc._track_gate_runs("NIFTY", prof, _df(t0), _fresh(80, 30), t0 + 180)
    clean = _card()
    veto, tag = svc._hypothesis_veto(_fresh(80, 30, clean), t0 + 180)
    assert veto and "WATCH" in veto
    assert tag and tag.startswith("confirm:")
    from app.paper.service import shadow_class

    class _X:
        hollow_reason = tag

    assert shadow_class(_X()) == "confirm"
    # Sub-floor AND first-bar: floor leads, no shadow for either ledger.
    weak = _card(vol_pts=2.0)
    veto2, tag2 = svc._hypothesis_veto(_fresh(80, 30, weak), t0 + 180)
    assert veto2 and "below the" in veto2 and "unconfirmed" in veto2
    assert tag2 is None
    print("  SHADOW -> confirm ledger tagged; confounded cards book nowhere")


def test_trace_records_confirm_shadow_class():
    """Review catch (major): _trace_bar's shadow field only knew late/refire
    and wrote confirm vetoes as 'floor' — corrupting the exact dataset the
    audits attribute vetoes from."""
    import tempfile
    from pathlib import Path

    import app.signals.service as sig_svc
    from app.signals.eval_trace import EvalTrace

    svc = _svc()
    prof = _profile(svc)
    now = int(time.time())
    df = pd.DataFrame([{"ts": now - 360, "open": 1, "high": 1, "low": 1,
                        "close": 24600.0, "volume": 10}])
    with tempfile.TemporaryDirectory() as d:
        tr = EvalTrace(Path(d) / "t.jsonl")
        prev = sig_svc.eval_trace
        sig_svc.eval_trace = tr
        try:
            svc._trace_bar("NIFTY", prof, df, _fresh(80, 30), None,
                           "WATCH — first bar", "confirm: WATCH — first bar",
                           now)
            rows = tr.load(days=1)
            assert rows and rows[0]["shadow"] == "confirm", rows
        finally:
            sig_svc.eval_trace = prev
    print("  TRACE  -> confirm vetoes recorded as confirm, never floor")


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
