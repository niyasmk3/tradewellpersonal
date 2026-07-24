"""Participation-floor tests: the volume/OI veto and its hollow counterfactual.

The floor withholds cards whose volume or OI component says nobody is in the
move; the paper book fills them anyway, tagged, so the ledger — not opinion —
decides whether the floor survives. These tests pin both halves: the veto
logic, and the strict separation of counterfactual rows from every aggregate
that grades the system's own decisions.

Run:  python backend/tests/test_hollow_floor.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

from app.config import Settings
from app.signals.models import (
    Action,
    Direction,
    ScoreBreakdown,
    ScoreComponent,
    SignalCard,
    SignalState,
    TradingMode,
)


def _cfg(**over):
    return Settings(_env_file=None, **over)


def _comps(vol=10.0, oi=15.0):
    return [
        ScoreComponent(name="Price action & structure", points=20, max=25),
        ScoreComponent(name="Trend & momentum", points=16, max=20),
        ScoreComponent(name="Volume confirmation", points=vol, max=15),
        ScoreComponent(name="Options & OI", points=oi, max=20),
        ScoreComponent(name="Volatility condition", points=10, max=10),
        ScoreComponent(name="News sentiment", points=4, max=10),
    ]


def _card(vol=10.0, oi=15.0, at=None, cid="H"):
    at = at or int(time.time())
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24000 CE", strike=24000.0, expiry="2026-07-30",
        entry_low=99.0, entry_high=101.0, premium_sl=92.0, target1=112.0,
        target2=125.0, trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=_comps(vol, oi), total=81.0),
        ref_entry_premium=100.0, lot_size=65, suggested_lots=1,
    )


class _Svc:
    """Just enough of SignalService for the unbound veto method."""
    def __init__(self, **over):
        self.cfg = _cfg(**over)


def _veto(card, **over):
    from app.signals.service import SignalService
    return SignalService._hollow_veto(_Svc(**over), card)


def test_floor_vetoes_hollow_components():
    # Defaults: volume floor 7/15, OI floor 8/20.
    v = _veto(_card(vol=2.0))
    assert v is not None and "Volume confirmation 2/15" in v and "7-point floor" in v, v
    o = _veto(_card(oi=4.0))
    assert o is not None and "Options & OI 4/20" in o and "8-point floor" in o, o
    # At the floor passes (floor is a minimum, not a ceiling on refusal).
    assert _veto(_card(vol=7.0)) is None
    assert _veto(_card(oi=8.0)) is None
    assert _veto(_card()) is None
    # 0 disables each floor independently.
    assert _veto(_card(vol=0.0), SIGNAL_MIN_VOLUME_SCORE=0) is None
    assert _veto(_card(oi=0.0), SIGNAL_MIN_OI_SCORE=0) is None
    # A card with no score breakdown is not this gate's business.
    bare = _card()
    bare.score = None
    assert _veto(bare) is None
    print("  FLOOR  -> vol 2/15 and oi 4/20 refused with spoken reasons; 0 disables")


def test_hollow_card_flows_to_paper_tagged():
    import tempfile
    from pathlib import Path

    from app.paper.service import PaperTradingService, is_hollow_row
    from app.state import MarketState
    from app.trades.store import TradeStore

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        state = MarketState()
        svc = PaperTradingService(_cfg(SIGNAL_MAX_PREMIUM_AGE_S=0), state, store)
        card = _card(vol=2.0)
        card.hollow_reason = "Volume confirmation 2/15 is below the 7-point floor"
        card.token = 777
        state.ticks[777] = {"last_price": 100.2}
        t = svc.consider(card)
        assert t is not None
        row = store.get(t.id)
        assert row.notes and row.notes.startswith("hollow:"), row.notes
        assert is_hollow_row(row)
        assert not is_hollow_row(_dummy_clean(store))
        print("  PAPER  -> hollow card fills with a persistent 'hollow:' tag")


def _dummy_clean(store):
    card = _card(cid="CLEAN")
    t = store.create_from_signal(card, 1, 100.0, 65)
    return store.get(t.id)


def test_hollow_capacity_is_separate_from_clean():
    import tempfile
    from pathlib import Path

    from app.paper.service import PaperTradingService
    from app.signals.risk_limits import risk_limit_store
    from app.state import MarketState
    from app.trades.store import TradeStore

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        state = MarketState()
        svc = PaperTradingService(_cfg(SIGNAL_MAX_PREMIUM_AGE_S=0), state, store)
        orig = risk_limit_store.effective
        risk_limit_store.effective = lambda c: {**orig(c), "max_open_positions": 1}
        try:
            # Clean intraday slot is FULL.
            _dummy_clean(store)
            # A hollow intraday card must still fill — its book is separate.
            h1 = _card(vol=2.0, cid="H1")
            h1.hollow_reason = "vol floor"
            h1.token = 777
            state.ticks[777] = {"last_price": 100.2}
            assert svc.consider(h1) is not None, "hollow must not queue behind clean"
            # But a SECOND hollow card queues behind the first hollow fill.
            h2 = _card(vol=2.0, cid="H2", at=int(time.time()) + 1)
            h2.hollow_reason = "vol floor"
            h2.token = 777
            svc.consider(h2)
            n_hollow = sum(1 for t in store.all()
                           if t.notes and t.notes.startswith("hollow:"))
            assert n_hollow == 1, n_hollow
        finally:
            risk_limit_store.effective = orig
    print("  SLOTS  -> hollow and clean books hold separate per-mode capacity")


def test_summary_and_evidence_exclude_hollow():
    import tempfile
    from pathlib import Path

    from app.paper.service import HONEST_FILLS_FROM, summarize
    from app.signals.calibration import _MIN_SAMPLES, _p75_mfe_pct
    from app.trades.store import TradeStore

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        # One clean winner, one hollow loser.
        clean = store.create_from_signal(_card(cid="C"), 1, 100.0, 65)
        store.auto_close(clean.id, 112.0, "target1")
        hollow = store.create_from_signal(_card(vol=2.0, cid="X"), 1, 100.0, 65)
        store.update(hollow.id, notes="hollow: vol floor")
        store.auto_close(hollow.id, 90.0, "stop")

        s = summarize(store)
        assert s["trades"] == 1, f"aggregates must count clean rows only: {s['trades']}"
        assert s["net_pnl"] > 0, "the hollow loss must not drag the clean book"
        assert s["hollow"] is not None and s["hollow"]["trades"] == 1
        assert s["hollow"]["net_pnl"] < 0 and s["hollow"]["expectancy"] < 0
        flagged = [r for r in s["rows"] if r["hollow"]]
        assert len(flagged) == 1 and flagged[0]["id"] == hollow.id

        # T1 calibration: 30 hollow samples are 0 samples.
        class _T:
            def __init__(self, hollow):
                self.mode = type("M", (), {"value": "intraday"})()
                self.status = type("S", (), {"value": "exited"})()
                self.entry_premium, self.mfe_premium = 100.0, 118.0
                self.entered_at = HONEST_FILLS_FROM + 3600
                self.excursion_from = self.entered_at
                self.notes = "hollow: vol floor" if hollow else None

        assert _p75_mfe_pct([_T(True)] * _MIN_SAMPLES) is None
        assert _p75_mfe_pct([_T(False)] * _MIN_SAMPLES) is not None
    print("  SPLIT  -> aggregates clean-only; hollow verdict block carries the loss")


def test_shadow_store_isolation():
    """Hollow cards stabilise in their own store: adopting one must not consume
    the real feed's throttle, and the real store must never serve it."""
    from app.signals.models import Bias, MarketStatus, Regime, SignalResponse
    from app.signals.store import SignalStore, ThrottleConfig

    real = SignalStore(store_path=None)
    shadow = SignalStore(store_path=None)
    now = int(time.time())
    card = _card(vol=2.0, at=now)
    card.hollow_reason = "vol floor"
    resp = SignalResponse(
        symbol="NIFTY", mode=TradingMode.INTRADAY, evaluated_at=now,
        status=MarketStatus(
            symbol="NIFTY", mode=TradingMode.INTRADAY, regime=Regime.MODERATE_BULLISH,
            regime_label="R", bias=Bias.BULLISH, bull_score=60, bear_score=40,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=card.action, signal=card, no_trade_reason=None, score=None,
    )
    cfg = ThrottleConfig(max_per_day=4, min_gap_s=0, cooldown_s=0)
    out = shadow.reconcile(resp, now, cfg)
    assert out.signal is not None and out.signal.hollow_reason == "vol floor"
    assert real.latest("NIFTY", TradingMode.INTRADAY) is None
    # The shadow adoption spent a shadow slot, not a real one.
    blocked = real._blocked("NIFTY:intraday", card, now, cfg)
    assert blocked is None, blocked
    print("  SHADOW -> hollow adoption spends no real-throttle slot, serves nothing")


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
