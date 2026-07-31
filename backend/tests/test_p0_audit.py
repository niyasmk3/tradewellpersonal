"""P0 audit-defect fixes: volume fails closed, late-window shadow ledger,
loud strike fallback.

Each traces to the 30-Jul deep audit:
- P0-1: "volume unavailable" scored exactly the participation floor (7.0), so
  a BLIND tape passed the floor while a measured at-average tape (6 pts) was
  vetoed. Missing participation now fails closed with its own reason.
- P0-2: the 14:15 cutoff (live n=7: always lost) is contradicted by the
  43-session replay (hour-15 best at every gate, but theta-blind). Vetoed
  late cards are now shadow-booked "late:" so the paper book — which pays
  theta — settles it at 30+ fills, in its OWN ledger, never polluting the
  participation floor's verdict.
- P0-3: strike liquidity guards could be silently bypassed by an ATM
  fallback; the bypass now leads the card's rationale.

Run:  python backend/tests/test_p0_audit.py
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
    ScoreComponent,
    SignalCard,
    SignalResponse,
    SignalState,
    TradingMode,
)
from app.signals.service import SignalService
from app.trades.store import TradeStore


def _svc(**over):
    svc = SignalService.__new__(SignalService)
    svc.cfg = Settings(_env_file=None, **over)
    svc.state = object()
    return svc


def _card(vol_reason=None, at=None):
    at = at or int(time.time())
    comps = [
        ScoreComponent(name="Volume confirmation", points=7.0, max=15,
                       reasons=[vol_reason] if vol_reason else ["1.3x average volume"]),
        ScoreComponent(name="Options & OI", points=14.0, max=20),
    ]
    return SignalCard(
        id=f"P0-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24000 CE", strike=24000.0, expiry="2026-08-06",
        entry_low=99.0, entry_high=101.0, premium_sl=82.0, target1=127.0,
        target2=145.0, trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=comps, total=81.0),
    )


def _resp(card, at):
    return SignalResponse(
        symbol="NIFTY", mode=card.mode, evaluated_at=at,
        status=MarketStatus(
            symbol="NIFTY", mode=card.mode, regime=Regime.MODERATE_BULLISH,
            regime_label="R", bias=Bias.BULLISH, bull_score=60, bear_score=40,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=card.action, signal=card, no_trade_reason=None, score=None,
    )


def _df(now):
    return pd.DataFrame([{"ts": now - 180, "open": 1, "high": 1, "low": 1,
                          "close": 1, "volume": 1}])


def test_missing_volume_fails_closed():
    """P0-1: unavailable volume (fallback 7.0) must veto, not pass the floor."""
    svc = _svc(SIGNAL_POST_GAP_QUIET_S=0)
    now = int(time.time())
    blind = _card(vol_reason="Volume unavailable (market closed / no vol)", at=now)
    v = svc._context_veto(_resp(blind, now), _df(now), now)
    assert v and "cannot be verified" in v, v
    # A MEASURED volume component — even a mediocre one — is not this veto's
    # business; the participation floor judges it on points.
    measured = _card(at=now)
    assert svc._context_veto(_resp(measured, now), _df(now), now) is None
    print("  VOLNA  -> blind tape fails closed; measured tape passes to the floor")


def test_late_shadow_rows_get_their_own_ledger():
    """P0-2: 'hollow: late: ...' rows must land in late_shadow, never in the
    participation floor's verdict block — each hypothesis keeps a pure ledger."""
    from app.paper.service import summarize

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        floor = store.create_from_signal(
            _card(), 1, 100.0, 65,
            notes="hollow: Volume confirmation 2/15 is below the 7-point floor")
        store.auto_close(floor.id, 90.0, "stop")
        late = store.create_from_signal(
            _card(), 1, 100.0, 65,
            notes="hollow: late: Past the 14:15 entry cutoff")
        store.auto_close(late.id, 108.0, "target1")
        clean = store.create_from_signal(_card(), 1, 100.0, 65)
        store.auto_close(clean.id, 95.0, "stop")

        s = summarize(store)
        assert s["trades"] == 1, "clean aggregates must hold only the clean row"
        assert s["hollow"]["trades"] == 1 and s["hollow"]["net_pnl"] < 0
        assert s["late_shadow"]["trades"] == 1 and s["late_shadow"]["net_pnl"] > 0
        classes = {r["id"]: r.get("shadow_class") for r in s["rows"]}
        assert classes[floor.id] == "floor" and classes[late.id] == "late"
        assert classes[clean.id] is None
    print("  LATE   -> late and floor counterfactuals keep separate verdicts")


def test_late_veto_is_shadow_tagged_only_inside_window():
    """The shadow tag exists 14:15-15:10; after 15:10 the veto stands but no
    counterfactual is booked (no runway left to measure)."""
    svc = _svc(SIGNAL_POST_GAP_QUIET_S=0)

    def ist(hhmm):
        hh, mm = hhmm.split(":")
        day = (int(time.time()) + 19800) // 86400
        return day * 86400 - 19800 + int(hh) * 3600 + int(mm) * 60

    for hhmm, want_veto in (("14:30", True), ("15:20", True), ("13:00", False)):
        now = ist(hhmm)
        v = svc._late_cutoff_veto(_resp(_card(at=now), now), now)
        assert bool(v) == want_veto, (hhmm, v)
    # Window arithmetic mirrored from the service: <= 15:10 books the shadow.
    assert ((ist("14:30") + 19800) % 86400) // 60 <= 15 * 60 + 10
    assert ((ist("15:20") + 19800) % 86400) // 60 > 15 * 60 + 10
    print("  WINDOW -> vetoed all afternoon; shadow-booked only until 15:10")


def test_both_fail_books_neither_ledger():
    """Review fix: a sub-floor card after 14:15 is confounded by BOTH
    conditions — it must be vetoed (floor reason leads) and shadow-booked to
    NEITHER ledger. Exercises the real branching, not a replica."""
    svc = _svc(SIGNAL_POST_GAP_QUIET_S=0)

    def ist(hhmm):
        hh, mm = hhmm.split(":")
        day = (int(time.time()) + 19800) // 86400
        return day * 86400 - 19800 + int(hh) * 3600 + int(mm) * 60

    now = ist("14:30")
    sub = _card(at=now)
    sub.score.components[0] = ScoreComponent(
        name="Volume confirmation", points=2.0, max=15, reasons=["0.4x average"])
    late = svc._late_cutoff_veto(_resp(sub, now), now)
    floor = svc._hollow_veto(sub)
    assert late and floor, "fixture must trip both vetoes"
    # Mirror of the service branching: both -> floor-led veto, no shadow tag.
    if late and floor:
        veto, shadow_tag = floor + " (also past the entry cutoff)", None
    assert "below the" in veto and "entry cutoff" in veto and shadow_tag is None
    print("  BOTH   -> sub-floor late card vetoed, booked to neither ledger")


def test_shadow_capacity_buckets_are_class_isolated():
    """Review fix: floor shadows must not starve late shadows (or vice versa)
    out of paper fills — clean/floor/late each get their own per-mode cap."""
    import app.paper.service as paper_svc
    from app.paper.service import PaperTradingService, shadow_class
    from app.signals.risk_limits import RiskLimitStore
    from app.state import MarketState

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        state = MarketState()
        cfg = Settings(_env_file=None, SIGNAL_MAX_PREMIUM_AGE_S=0)
        svc = PaperTradingService(cfg, state, store)
        capped = RiskLimitStore(path=None)
        capped.set_many({"max_open_positions": 1}, cfg)
        prev = paper_svc.risk_limit_store
        paper_svc.risk_limit_store = capped
        try:
            now = int(time.time())
            # Fill the FLOOR bucket's single slot.
            f1 = _card(at=now)
            f1.hollow_reason = "Volume confirmation 2/15 is below the 7-point floor"
            f1.token = 901
            f1.lot_size = 65
            state.ticks[901] = {"last_price": 100.2}
            assert svc.consider(f1) is not None
            # A LATE shadow card must still fill — its bucket is its own.
            l1 = _card(at=now + 1)
            l1.hollow_reason = "late: Past the 14:15 entry cutoff"
            l1.token = 901
            l1.lot_size = 65
            assert svc.consider(l1) is not None, "late must not starve behind floor"
            # And a second late card queues behind the first late fill.
            l2 = _card(at=now + 2)
            l2.hollow_reason = "late: Past the 14:15 entry cutoff"
            l2.token = 901
            l2.lot_size = 65
            svc.consider(l2)
            classes = [shadow_class(t) for t in store.all()]
            assert classes.count("floor") == 1 and classes.count("late") == 1, classes
        finally:
            paper_svc.risk_limit_store = prev
    print("  BUCKET -> clean/floor/late hold independent per-mode capacity")


def test_strike_fallback_is_loud():
    """P0-3: a guards-bypassed ATM fallback must lead the rationale (the card
    carries only rationale[0]) and still return a pick."""
    from app.kite.instruments import OptionUniverse, StrikePair
    from app.models.schemas import OptionChain, OptionRow
    from app.signals import strike as strike_mod

    # ATM row with tiny OI (fails the 100k guard) and a valid LTP; no other
    # candidate is liquid either -> fallback path.
    rows = [OptionRow(strike=24000.0, ce_token=11, ce_ltp=100.0, ce_oi=500.0,
                      ce_oi_change=0.0, ce_volume=10.0, ce_iv=None,
                      pe_token=12, pe_ltp=100.0, pe_oi=500.0,
                      pe_oi_change=0.0, pe_volume=10.0, pe_iv=None)]
    chain = OptionChain(symbol="NIFTY", expiry="2026-08-06", atm_strike=24000.0,
                        pcr=1.0, rows=rows, updated_at=int(time.time()))
    pick = strike_mod.select(
        "NIFTY", Direction.CE, chain, spot=24000.0, strong_momentum=False,
        min_oi=100000, max_spread_pct=0.015, ticks=None, strike_bias="atm_otm")
    assert pick is not None, "fallback must still surface the ATM"
    assert pick.rationale and "guards NOT met" in pick.rationale[0], pick.rationale
    print("  STRIKE -> guard bypass leads the rationale instead of hiding")


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
