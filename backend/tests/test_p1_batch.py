"""P1 audit batch: stop-basis paired A/B, calibration T1 floor guard, and the
per-bar blind-spot eval trace.

Each traces to the 30-Jul deep audit's P1 plan:
- P1-5: STOP_PRIMARY was flipped twice on single-trade evidence. Every clean
  paper fill now books a twin running the OTHER stop basis; summarize() pairs
  them by signal id and rules at 30+ DIVERGED pairs.
- P1-6: at n>=30 the T1 calibrator would land near the P75 MFE (~+10%) and
  price_ladder silently NULLS any quick target >= T1 — collapsing the
  two-stage exit. Calibrated T1 is floored at 1.5x the quick target.
- P1-2: 36/41 missed moves had no candidate and 27 were unattributable —
  the engine never recorded what it scored and declined. One JSONL line per
  closed bar per mode now records both directions' scores and the veto.

Run:  python backend/tests/test_p1_batch.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
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
from app.state import MarketState
from app.trades.store import TradeStore


def _card(cid="P1", at=None, hollow=None, entry=100.0):
    at = at or int(time.time())
    comps = [
        ScoreComponent(name="Volume confirmation", points=9.0, max=15,
                       reasons=["1.4x average volume"]),
        ScoreComponent(name="Options & OI", points=14.0, max=20),
    ]
    c = SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24000 CE", strike=24000.0, expiry="2026-08-06",
        entry_low=entry - 1, entry_high=entry + 1,
        premium_sl=round(entry * 0.82, 2), target1=round(entry * 1.27, 2),
        target2=round(entry * 1.45, 2), trailing_sl_rule="r", risk_reward=1.5,
        confidence=81.0, underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=comps, total=81.0),
    )
    if hollow:
        c.hollow_reason = hollow
    c.token = 901
    c.lot_size = 65
    return c


def _paper_svc(store, **over):
    from app.paper.service import PaperTradingService

    cfg = Settings(_env_file=None, SIGNAL_MAX_PREMIUM_AGE_S=0, **over)
    state = MarketState()
    state.ticks[901] = {"last_price": 100.2}
    return PaperTradingService(cfg, state, store), state


# ---------------------------------------------------------------- P1-5 -----

def test_stop_ab_twin_books_the_opposite_basis():
    """A clean fill under premium-primary books an underlying-primary twin
    (disaster backstop set); under underlying-primary, a premium-primary twin
    (no backstop). Notes record the twin's own basis for pair labelling."""
    from app.paper.service import shadow_class

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        svc, _ = _paper_svc(store, TRADING_CAPITAL=500000, STOP_PRIMARY="premium")
        svc.consider(_card("A"))
        rows = store.all()
        assert len(rows) == 2, [t.notes for t in rows]
        clean = next(t for t in rows if shadow_class(t) is None)
        twin = next(t for t in rows if shadow_class(t) == "stopb")
        assert clean.disaster_sl is None
        assert twin.disaster_sl is not None
        assert (twin.notes or "").startswith("hollow: stopb: underlying")
        assert twin.signal_id == clean.signal_id, "pairing key must match"
        assert twin.entry_premium == clean.entry_premium, "paired entries must match"

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        svc, _ = _paper_svc(store, TRADING_CAPITAL=500000, STOP_PRIMARY="underlying")
        svc.consider(_card("B"))
        clean = next(t for t in store.all() if shadow_class(t) is None)
        twin = next(t for t in store.all() if shadow_class(t) == "stopb")
        assert clean.disaster_sl is not None, "underlying-primary live arm has the backstop"
        assert twin.disaster_sl is None, "premium-primary twin must not"
        assert (twin.notes or "").startswith("hollow: stopb: premium")
    print("  TWIN   -> each clean fill pairs with the opposite stop basis")


def test_stop_ab_twin_gating():
    """No twin when: the flag is off, capital is unset (the two bases produce
    identical rows — nothing to measure), or the card is itself a shadow."""
    from app.paper.service import shadow_class

    for kwargs, card in (
        (dict(TRADING_CAPITAL=500000, STOP_AB_PAIRED=False), _card("C")),
        (dict(TRADING_CAPITAL=0), _card("D")),
        (dict(TRADING_CAPITAL=500000), _card("E", hollow="Volume confirmation 2/15 is below the 7-point floor")),
    ):
        with tempfile.TemporaryDirectory() as d:
            store = TradeStore(path=Path(d) / "p.json")
            svc, _ = _paper_svc(store, STOP_PRIMARY="premium", **kwargs)
            svc.consider(card)
            assert not [t for t in store.all() if shadow_class(t) == "stopb"], kwargs
    print("  GATE   -> flag off / no capital / shadow cards book no twin")


def test_stop_ab_twin_exempt_from_capacity():
    """The twin exists 1:1 with a clean fill that already cleared its bucket —
    it must neither consume a clean slot nor be blocked by one."""
    import app.paper.service as paper_svc
    from app.paper.service import shadow_class
    from app.signals.risk_limits import RiskLimitStore

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        svc, state = _paper_svc(store, TRADING_CAPITAL=500000, STOP_PRIMARY="premium")
        capped = RiskLimitStore(path=None)
        capped.set_many({"max_open_positions": 1}, svc.cfg)
        prev = paper_svc.risk_limit_store
        paper_svc.risk_limit_store = capped
        try:
            svc.consider(_card("F"))
            assert len(store.all()) == 2, "clean + twin despite cap=1"
            # A second clean card defers on the FULL clean bucket — and no
            # orphan twin appears for a fill that never happened.
            svc.consider(_card("G", at=int(time.time()) + 1))
            assert len(store.all()) == 2
            classes = sorted(str(shadow_class(t)) for t in store.all())
            assert classes == ["None", "stopb"], classes
        finally:
            paper_svc.risk_limit_store = prev
    print("  CAP    -> twin rides its clean fill's slot; blocked fills spawn none")


def test_stop_ab_summary_pairs_and_verdict():
    """summarize() pairs clean row and twin by signal id, counts divergence,
    splits arms by the twin's recorded basis, and keeps stopb rows out of
    every other ledger (clean aggregates, floor, late)."""
    from app.paper.service import summarize

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        card = _card("H")
        clean = store.create_from_signal(card, 1, 100.0, 65)
        twin = store.create_from_signal(
            card, 1, 100.0, 65,
            notes="hollow: stopb: underlying — stop-basis A/B twin (live basis: premium)")
        # Premium arm stops at the 18% level; underlying arm survives to +8%.
        store.auto_close(clean.id, 82.0, "stop")
        store.auto_close(twin.id, 108.0, "time_exit")

        s = summarize(store)
        assert s["trades"] == 1, "clean aggregates must hold only the clean row"
        assert s["hollow"] is None and s["late_shadow"] is None, \
            "stopb rows must not leak into the floor/late ledgers"
        ab = s["stop_ab"]
        assert ab is not None and ab["n"] == 1 and ab["n_diverged"] == 1
        assert ab["premium_stop"]["net_pnl"] < 0 < ab["underlying_stop"]["net_pnl"]
        assert "evidence gathering" in ab["verdict"] or "30-pair" in ab["verdict"]
        classes = {r["id"]: r["shadow_class"] for r in s["rows"]}
        assert classes[clean.id] is None and classes[twin.id] == "stopb"

        # An unsettled pair (twin still open) is pending, not silently gone.
        card2 = _card("I", at=int(time.time()) + 5)
        c2 = store.create_from_signal(card2, 1, 100.0, 65)
        store.create_from_signal(
            card2, 1, 100.0, 65,
            notes="hollow: stopb: underlying — stop-basis A/B twin (live basis: premium)")
        store.auto_close(c2.id, 82.0, "stop")
        s = summarize(store)
        assert s["stop_ab"]["pending"] == 1 and s["stop_ab"]["n"] == 1
    print("  PAIR   -> arms split by recorded basis; open pairs stay visible")


def test_stop_ab_pending_is_honest_era_scoped():
    """Review catch: a pre-honest-era pair must vanish from pending too, not
    linger as a phantom that can never clear."""
    from app.paper.service import HONEST_FILLS_FROM, summarize

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        card = _card("J")
        c = store.create_from_signal(card, 1, 100.0, 65)
        tw = store.create_from_signal(
            card, 1, 100.0, 65,
            notes="hollow: stopb: underlying — stop-basis A/B twin (live basis: premium)")
        store.auto_close(c.id, 82.0, "stop")
        store.auto_close(tw.id, 108.0, "time_exit")
        for tid in (c.id, tw.id):
            store._trades[tid].entered_at = HONEST_FILLS_FROM - 86400
        s = summarize(store)
        assert s["stop_ab"] is None, \
            "pre-honest pair must appear in NEITHER settled nor pending"
    print("  ERA    -> pending lives in the same honest-era universe as n")


# ---------------------------------------------------------------- P1-6 -----

def _seed_calibration_store(store, mfe_pct, n=32):
    """n clean, honest, from-entry-tracked intraday fills peaking at mfe_pct."""
    from app.paper.service import HONEST_FILLS_FROM

    for i in range(n):
        t = store.create_from_signal(_card(f"K{i}", at=int(time.time()) + i), 1, 100.0, 65)
        store.auto_close(t.id, 100.0 + i * 0.001, "time_exit")
        row = store._trades[t.id]
        row.entered_at = HONEST_FILLS_FROM + 1000 + i
        row.mfe_premium = round(100.0 * (1 + mfe_pct), 2)
        row.excursion_from = row.entered_at + 5


def test_calibration_t1_floored_at_quick_target_multiple():
    """A P75 MFE below 1.5x the quick target must calibrate to the floor —
    otherwise price_ladder nulls the quick target and the two-stage exit
    collapses (the exact silent failure the audit flagged)."""
    from app.services import feed
    from app.signals import calibration
    from app.signals.modes import build_profiles
    from app.signals.risk import price_ladder

    cfg = Settings(_env_file=None, QUICK_TARGET_PCT=0.12)
    profile = build_profiles(cfg)[TradingMode.INTRADAY]

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        _seed_calibration_store(store, mfe_pct=0.10)   # P75 ~ +10% < 18% floor
        prev = getattr(feed, "paper_store", None)
        feed.paper_store = store
        calibration._cache = {"at": 0.0, "value": None}
        try:
            rr1 = calibration.intraday_rr1(profile, cfg)
            assert rr1 is not None, "30+ clean samples must activate the calibrator"
            t1_pct = rr1 * profile.premium_sl_pct
            assert abs(t1_pct - 0.18) < 1e-6, f"expected the 0.18 floor, got {t1_pct}"
            # The invariant the floor exists for: the quick target SURVIVES.
            ladder = price_ladder(100.0, profile.premium_sl_pct, rr1,
                                  profile.rr_target2, None, cfg.quick_target_pct)
            assert ladder["quick_target"] is not None, \
                "calibrated ladder must keep the two-stage exit"
        finally:
            feed.paper_store = prev
            calibration._cache = {"at": 0.0, "value": None}
    print("  FLOOR  -> P75 below 1.5x QT calibrates to the floor; QT survives")


def test_calibration_floor_never_exceeds_the_clamp_ceiling():
    """Review catch: QUICK_TARGET_PCT > 18% would push the 1.5x floor past
    the 27% ceiling — the clamp must stay the hard bound."""
    from app.services import feed
    from app.signals import calibration
    from app.signals.modes import build_profiles

    cfg = Settings(_env_file=None, QUICK_TARGET_PCT=0.20)   # 1.5x = 30% > 27%
    profile = build_profiles(cfg)[TradingMode.INTRADAY]

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        _seed_calibration_store(store, mfe_pct=0.10)
        prev = getattr(feed, "paper_store", None)
        feed.paper_store = store
        calibration._cache = {"at": 0.0, "value": None}
        try:
            rr1 = calibration.intraday_rr1(profile, cfg)
            assert rr1 is not None
            t1_pct = rr1 * profile.premium_sl_pct
            assert abs(t1_pct - 0.27) < 1e-6, \
                f"floor must cap at the 27% ceiling, got {t1_pct}"
        finally:
            feed.paper_store = prev
            calibration._cache = {"at": 0.0, "value": None}
    print("  CEIL   -> the 27% clamp outranks the quick-target floor")


def test_calibration_above_floor_untouched():
    """A P75 comfortably above the floor calibrates to the data, not the floor."""
    from app.services import feed
    from app.signals import calibration
    from app.signals.modes import build_profiles

    cfg = Settings(_env_file=None, QUICK_TARGET_PCT=0.12)
    profile = build_profiles(cfg)[TradingMode.INTRADAY]

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        _seed_calibration_store(store, mfe_pct=0.22)
        prev = getattr(feed, "paper_store", None)
        feed.paper_store = store
        calibration._cache = {"at": 0.0, "value": None}
        try:
            rr1 = calibration.intraday_rr1(profile, cfg)
            assert rr1 is not None
            t1_pct = rr1 * profile.premium_sl_pct
            assert abs(t1_pct - 0.22) < 1e-3, f"expected the data's 0.22, got {t1_pct}"
        finally:
            feed.paper_store = prev
            calibration._cache = {"at": 0.0, "value": None}
    print("  DATA   -> above the floor, the excursion evidence governs")


# ---------------------------------------------------------------- P1-2 -----

def test_eval_trace_once_per_bar_and_prune():
    from app.signals.eval_trace import EvalTrace

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "trace.jsonl"
        tr = EvalTrace(p)
        assert not p.exists(), "construction must perform no I/O"
        now = int(time.time())
        e = {"ts": now, "bar": 1000, "symbol": "NIFTY", "mode": "intraday",
             "bull": 70.0, "bear": 40.0, "regime": "moderate_bullish"}
        tr.record(e)
        tr.record(dict(e, ts=now + 5, bull=70.0))       # same bar: dropped
        tr.record(dict(e, bar=1180, ts=now + 180))      # next bar: kept
        tr.record(dict(e, mode="scalp"))                # other mode: own key
        lines = p.read_text().splitlines()
        assert len(lines) == 3, lines

        rows = tr.load(days=1, symbol="NIFTY", mode="intraday")
        assert [r["bar"] for r in rows] == [1000, 1180]
        assert tr.load(days=1, mode="scalp")[0]["mode"] == "scalp"

        # Prune: an old line goes, recent stay; 0 empties the file.
        with open(p, "a") as fh:
            fh.write(json.dumps(dict(e, ts=now - 40 * 86400, bar=1)) + "\n")
        tr.prune(30)
        assert len(p.read_text().splitlines()) == 3
        tr.prune(0)
        assert p.read_text().strip() == ""
    print("  TRACE  -> once per (symbol, mode, bar); prune honours retention")


def _fresh(action=Action.WAIT, reason=None, card=None):
    status = MarketStatus(
        symbol="NIFTY", mode=TradingMode.INTRADAY, regime=Regime.SIDEWAYS,
        regime_label="R", bias=Bias.NEUTRAL, bull_score=61.0, bear_score=44.0,
        headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
    )
    return SignalResponse(symbol="NIFTY", mode=TradingMode.INTRADAY,
                          evaluated_at=int(time.time()), status=status,
                          action=action, signal=card, no_trade_reason=reason,
                          score=None)


def test_trace_bar_hook_records_veto_and_shadow():
    """The service hook writes the definitive per-bar line: scores from the
    status, the veto verbatim, the shadow class, and the held-vs-fresh card
    ids that make cadence suppression visible."""
    import app.signals.service as sig_svc
    from app.signals.eval_trace import EvalTrace
    from app.signals.service import SignalService

    svc = SignalService.__new__(SignalService)
    svc.cfg = Settings(_env_file=None)
    now = int(time.time())
    df = pd.DataFrame([{"ts": now - 360, "open": 1, "high": 1, "low": 1,
                        "close": 24400.0, "volume": 10},
                       {"ts": now - 180, "open": 1, "high": 1, "low": 1,
                        "close": 24410.5, "volume": 12}])
    with tempfile.TemporaryDirectory() as d:
        tr = EvalTrace(Path(d) / "t.jsonl")
        prev = sig_svc.eval_trace
        sig_svc.eval_trace = tr
        try:
            from app.signals.modes import build_profiles
            profile = build_profiles(svc.cfg)[TradingMode.INTRADAY]
            held = _card("HELD")
            svc._trace_bar("NIFTY", profile, df, _fresh(reason="veto text"),
                           _fresh(card=held), "veto text",
                           "late: Past the 14:15 entry cutoff", now)
            rows = tr.load(days=1)
            assert len(rows) == 1
            r = rows[0]
            assert r["bar"] == now - 180 and r["bull"] == 61.0 and r["bear"] == 44.0
            assert r["veto"] == "veto text" and r["shadow"] == "late"
            assert r["card"] is None and r["held"] == held.id
            assert r["close"] == 24410.5

            # Retention 0 disables the hook entirely.
            svc.cfg = Settings(_env_file=None, EVAL_TRACE_DAYS=0)
            svc._trace_bar("NIFTY", profile, df, _fresh(), None, None, None, now + 180)
            assert len(tr.load(days=1)) == 1
        finally:
            sig_svc.eval_trace = prev
    print("  HOOK   -> per-bar line carries scores, veto, shadow, held card")


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
