"""Re-fire-guard shadow ledger (03-Aug): the guard's blocks become graded
counterfactuals instead of vanished WAITs.

Born from a live event: 10:02 NIFTY 24550 CE filled 75.70, the invalidation
broke for ONE bar at 10:03 (a shake-out; -Rs282), the guard armed for 2h, and
when the thesis re-cleared the gate at 10:57 the premium ran 84 -> 93.9
unmeasured. The guard's founding evidence (28-Jul: two re-fires, both lost)
is n=2; the counter-example is n=1. Neither decides a knob — so refire joins
floor and late as a measured hypothesis with its own shadow class, capacity
bucket and 30-fill verdict.

These tests exercise the REAL resolution method (_hypothesis_veto), with the
guard genuinely armed through the paper book — not a mirrored replica of the
branching.

Run:  python backend/tests/test_refire_shadow.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
import time
from pathlib import Path

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


def _ist_epoch(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    day = (int(time.time()) + 19800) // 86400
    return day * 86400 - 19800 + int(hh) * 3600 + int(mm) * 60


def _card(at, strike=24550.0, vol_points=9.0, mode=TradingMode.INTRADAY):
    comps = [
        ScoreComponent(name="Volume confirmation", points=vol_points, max=15,
                       reasons=["1.4x average volume"]),
        ScoreComponent(name="Options & OI", points=14.0, max=20),
    ]
    return SignalCard(
        id=f"RF-{at}-{strike}", symbol="NIFTY", mode=mode, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract=f"NIFTY {strike:.0f} CE", strike=strike, expiry="2026-08-04",
        entry_low=84.0, entry_high=86.0, premium_sl=70.0, target1=108.0,
        target2=123.0, trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=comps, total=81.0),
    )


def _resp(card, at):
    return SignalResponse(
        symbol="NIFTY", mode=card.mode, evaluated_at=at,
        status=MarketStatus(
            symbol="NIFTY", mode=card.mode, regime=Regime.MODERATE_BULLISH,
            regime_label="R", bias=Bias.BULLISH, bull_score=81, bear_score=24,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=card.action, signal=card, no_trade_reason=None, score=None,
    )


def _svc(**over):
    svc = SignalService.__new__(SignalService)
    # SIGNAL_CONFIRM_BARS=0: these tests exercise the re-fire hypothesis IN
    # ISOLATION; with the WATCH->CONFIRM gate (05-Aug) enabled, a fixture
    # card with no tracked gate-run would be confounded by the confirm veto
    # and every "refire alone" assertion would silently test the wrong thing.
    over.setdefault("SIGNAL_CONFIRM_BARS", 0)
    svc.cfg = Settings(_env_file=None, SIGNAL_POST_GAP_QUIET_S=0, **over)
    svc.state = object()
    return svc


class _armed_guard:
    """Context manager: a paper 24550 CE invalidation-close `age_s` ago, wired
    where _recent_rejections actually reads — the REAL arming path."""

    def __init__(self, now, age_s=3540):
        self.now, self.age_s = now, age_s

    def __enter__(self):
        import app.trades.store as ts_mod
        from app.services import feed

        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.paper = TradeStore(path=d / "p.json")
        t = self.paper.create_from_signal(_card(self.now - self.age_s - 60), 1, 75.7, 65)
        self.paper.auto_close(t.id, 72.26, "invalidation")
        self.paper._trades[t.id].exited_at = self.now - self.age_s
        self._orig = (ts_mod.trade_store, getattr(feed, "paper_store", None))
        ts_mod.trade_store = TradeStore(path=d / "l.json")
        feed.paper_store = self.paper
        return self

    def __exit__(self, *a):
        import app.trades.store as ts_mod
        from app.services import feed

        ts_mod.trade_store, feed.paper_store = self._orig
        self.tmp.cleanup()


def test_refire_alone_is_shadow_booked():
    """The 03-Aug event's shape: guard armed, clean card at 10:57 — vetoed,
    and now shadow-tagged 'refire:' so the block gets graded."""
    now = _ist_epoch("10:57")
    with _armed_guard(now):
        svc = _svc()
        veto, tag = svc._hypothesis_veto(_resp(_card(now), now), now)
        assert veto and veto.startswith("Re-fire guard:"), veto
        assert tag == "refire: " + veto, tag
    print("  REFIRE -> guard veto stands AND leaves a graded shadow")


def test_confounded_cards_book_no_ledger():
    """A card failing two hypotheses is confounded evidence for both: vetoed
    with the right precedence text, shadow-booked NOWHERE."""
    # refire + late (14:30, guard armed 13:35)
    now = _ist_epoch("14:30")
    with _armed_guard(now, age_s=3300):
        svc = _svc()
        veto, tag = svc._hypothesis_veto(_resp(_card(now), now), now)
        assert veto.startswith("Re-fire guard:") and veto.endswith(
            "(also past the entry cutoff)"), veto
        assert tag is None
        # all three (sub-floor volume, guard armed, after the cutoff):
        # floor leads and both other gates are named.
        sub = _card(now, vol_points=2.0)
        veto, tag = svc._hypothesis_veto(_resp(sub, now), now)
        assert "below the" in veto and "under the re-fire guard" in veto, veto
        assert "past the entry cutoff" in veto, "all three must be named"
        assert tag is None
    # refire + floor only (mid-morning, before the cutoff): floor leads.
    am = _ist_epoch("11:00")
    with _armed_guard(am, age_s=3000):
        svc = _svc()
        sub_am = _card(am, vol_points=2.0)
        veto, tag = svc._hypothesis_veto(_resp(sub_am, am), am)
        assert "below the" in veto and veto.endswith("(also under the re-fire guard)"), veto
        assert tag is None
    # floor + late without refire: the P0 text, verbatim (regression).
    now2 = _ist_epoch("14:30")
    svc2 = _svc(SIGNAL_REFIRE_GUARD_S=0)
    sub2 = _card(now2, vol_points=2.0)
    veto, tag = svc2._hypothesis_veto(_resp(sub2, now2), now2)
    assert veto.endswith(" (also past the entry cutoff)") and "below the" in veto, veto
    assert tag is None
    print("  MIXED  -> confounded cards vetoed with precedence, booked nowhere")


def test_single_hypothesis_regressions_unchanged():
    """Floor-only and late-only behave exactly as before the refire class."""
    svc = _svc(SIGNAL_REFIRE_GUARD_S=0)
    # floor only
    now = _ist_epoch("11:00")
    sub = _card(now, vol_points=2.0)
    veto, tag = svc._hypothesis_veto(_resp(sub, now), now)
    assert "below the" in veto and tag == veto
    # late only, inside the shadow window
    late_t = _ist_epoch("14:30")
    veto, tag = svc._hypothesis_veto(_resp(_card(late_t), late_t), late_t)
    assert "entry cutoff" in veto and tag == "late: " + veto
    # late only, past 15:10 — veto stands, no shadow (no runway)
    very_late = _ist_epoch("15:20")
    veto, tag = svc._hypothesis_veto(_resp(_card(very_late), very_late), very_late)
    assert veto and tag is None
    # nothing fails -> clean pass
    veto, tag = svc._hypothesis_veto(_resp(_card(now), now), now)
    assert veto is None and tag is None
    print("  REGRESS-> floor/late semantics byte-identical to P0")


def test_shadow_class_and_summary_ledger():
    """'hollow: refire:' rows land in refire_shadow — never in floor/late —
    and the class is recognised on both Trade notes and card hollow_reason."""
    from app.paper.service import shadow_class, summarize

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        now = int(time.time())
        r = store.create_from_signal(
            _card(now), 1, 85.0, 65,
            notes="hollow: refire: Re-fire guard: NIFTY 24550 CE invalidated 54m ago")
        store.auto_close(r.id, 93.0, "target1")
        clean = store.create_from_signal(_card(now + 1, strike=24600.0), 1, 85.0, 65)
        store.auto_close(clean.id, 80.0, "stop")

        s = summarize(store)
        assert s["trades"] == 1
        assert s["refire_shadow"]["trades"] == 1 and s["refire_shadow"]["net_pnl"] > 0
        assert s["hollow"] is None and s["late_shadow"] is None
        classes = {x["id"]: x["shadow_class"] for x in s["rows"]}
        assert classes[r.id] == "refire" and classes[clean.id] is None

    card = _card(int(time.time()))
    card.hollow_reason = "refire: Re-fire guard: ..."
    assert shadow_class(card) == "refire"
    print("  LEDGER -> refire rows keep their own verdict block")


def test_refire_capacity_bucket_is_isolated():
    """A refire shadow must not starve (or be starved by) floor shadows or
    clean fills — same per-mode-per-class rule as late/floor."""
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
            state.ticks[901] = {"last_price": 85.0}
            f1 = _card(now)
            f1.hollow_reason = "Volume confirmation 2/15 is below the 7-point floor"
            f1.token, f1.lot_size = 901, 65
            assert svc.consider(f1) is not None
            r1 = _card(now + 1, strike=24600.0)
            r1.hollow_reason = "refire: Re-fire guard: NIFTY 24550 CE stopped out 30m ago"
            r1.token, r1.lot_size = 901, 65
            assert svc.consider(r1) is not None, "refire must not starve behind floor"
            r2 = _card(now + 2, strike=24650.0)
            r2.hollow_reason = "refire: Re-fire guard: NIFTY 24550 CE stopped out 31m ago"
            r2.token, r2.lot_size = 901, 65
            svc.consider(r2)
            classes = [shadow_class(t) for t in store.all()]
            assert classes.count("floor") == 1 and classes.count("refire") == 1, classes
        finally:
            paper_svc.risk_limit_store = prev
    print("  BUCKET -> refire holds its own per-mode capacity slot")


def test_shadow_classes_never_share_a_store_slot():
    """Review catch (03-Aug): with one shared shadow store, a squatting floor
    card silently DROPPED same-direction refire/late candidates (a SignalStore
    holds one active card per symbol+mode and never displaces an incumbent
    with a same-direction candidate). Each class now routes to its own store,
    so every hypothesis keeps its own slot. Driven through reconcile() — the
    real adoption layer — not through consider()."""
    from app.signals.store import SignalStore, ThrottleConfig, shadow_store_for

    # The router: one store per class, all distinct.
    floor_tag = "Volume confirmation 2/15 is below the 7-point floor"
    stores = {t: shadow_store_for(t) for t in
              (floor_tag, "refire: Re-fire guard: ...", "late: Past the cutoff")}
    assert len({id(s) for s in stores.values()}) == 3, "classes must not share a store"

    # The starvation repro, on isolated stores of the same shape: a floor
    # shadow adopts; a same-direction refire candidate arrives while it is
    # active. With separate stores BOTH are adopted and served.
    now = int(time.time())
    cfg = ThrottleConfig(max_per_day=50, min_gap_s=0, cooldown_s=0, flip_guard_s=0)
    floor_st, refire_st = SignalStore(store_path=None), SignalStore(store_path=None)
    f = _card(now)
    f.hollow_reason = floor_tag
    floor_st.reconcile(_resp(f, now), now, cfg)
    r = _card(now + 30, strike=24600.0)
    r.hollow_reason = "refire: Re-fire guard: NIFTY 24550 CE invalidated 54m ago"
    refire_st.reconcile(_resp(r, now + 30), now + 30, cfg)
    served_f = floor_st.latest("NIFTY", TradingMode.INTRADAY)
    served_r = refire_st.latest("NIFTY", TradingMode.INTRADAY)
    assert served_f.signal is not None and served_f.signal.id == f.id
    assert served_r.signal is not None and served_r.signal.id == r.id, \
        "refire candidate must hold its own slot, not vanish behind floor's"
    print("  SLOT   -> each hypothesis owns its store slot; no silent drops")


def test_refire_shadow_fills_do_not_rearm_the_guard():
    """The loop that must not exist: a refire shadow stops out -> that stop
    must NOT arm the guard again (hollow rows never count as rejections)."""
    import app.trades.store as ts_mod
    from app.services import feed

    now = int(time.time())
    with tempfile.TemporaryDirectory() as d:
        paper = TradeStore(path=Path(d) / "p.json")
        sh = paper.create_from_signal(
            _card(now - 900), 1, 85.0, 65,
            notes="hollow: refire: Re-fire guard: NIFTY 24550 CE stopped out 40m ago")
        paper.auto_close(sh.id, 70.0, "stop")
        paper._trades[sh.id].exited_at = now - 600
        orig = (ts_mod.trade_store, getattr(feed, "paper_store", None))
        ts_mod.trade_store = TradeStore(path=Path(d) / "l.json")
        feed.paper_store = paper
        try:
            svc = _svc()
            assert svc._refire_veto(_card(now), now) is None
        finally:
            ts_mod.trade_store, feed.paper_store = orig
    print("  NOLOOP -> shadow stops never re-arm the guard")


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
