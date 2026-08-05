"""Stop-calibration shadow (05-Aug): MAE-derived SL fit, the twin that moves
ONLY the stop, and the stop_calib paired ledger.

Run:  python backend/tests/test_stop_calib.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from app.signals.calibration import (
    _SL_BUFFER, _SL_CLAMP_LO, _winner_mae_sl_pct, intraday_sl_pct,
)

NOW = int(time.time())


def _fill(entry=100.0, exit_p=110.0, mae=95.0, mode="intraday", notes=None):
    return SimpleNamespace(
        mode=SimpleNamespace(value=mode), status=SimpleNamespace(value="exited"),
        entry_premium=entry, exit_premium=exit_p, mae_premium=mae,
        entered_at=NOW, excursion_from=NOW + 5, notes=notes,
    )


def test_sl_calibration_needs_evidence():
    # 29 clean fills: under the 30-fill bar, regardless of winners.
    assert _winner_mae_sl_pct([_fill() for _ in range(29)]) is None
    # 30 fills but only 9 winners: a P90 of 9 is noise wearing a percentile.
    rows = [_fill(exit_p=110.0) for _ in range(9)] + [_fill(exit_p=90.0) for _ in range(21)]
    assert _winner_mae_sl_pct(rows) is None
    # Hollow rows and other modes never feed the fit — no twin feedback loop.
    rows = [_fill(notes="hollow: stopc: twin") for _ in range(40)]
    assert _winner_mae_sl_pct(rows) is None
    print("  GATE   -> None until 30 clean fills with 10+ winners; shadows excluded")


def test_sl_calibration_fits_winners_mae():
    # 20 winners bottoming 12% below entry, 10 losers: P90 of winner MAE = 0.12,
    # + 3pt buffer = 0.15 — the stop sits below where winners bottom.
    rows = ([_fill(exit_p=115.0, mae=88.0) for _ in range(20)]
            + [_fill(exit_p=80.0, mae=75.0) for _ in range(10)])
    got = _winner_mae_sl_pct(rows)
    assert got is not None and abs(got - (0.12 + _SL_BUFFER)) < 1e-9, got
    # Winners that never dipped clamp to the floor, not to a 3% hair-trigger.
    rows = ([_fill(exit_p=115.0, mae=100.0) for _ in range(20)]
            + [_fill(exit_p=80.0, mae=75.0) for _ in range(10)])
    assert _winner_mae_sl_pct(rows) == _SL_CLAMP_LO
    print("  FIT    -> P90 winner MAE + buffer, clamped to [10%, 25%]")


def test_sl_calibration_flag_gate():
    from app.signals import calibration as cal

    cal._sl_cache["at"] = 0.0  # a stale cache must not mask the flag
    off = SimpleNamespace(stop_calib_shadow=False)
    assert intraday_sl_pct(off) is None
    print("  FLAG   -> STOP_CALIB_SHADOW=false keeps the calibrator silent")


def _card(entry=100.0):
    from app.signals.models import (
        Action, Direction, ScoreBreakdown, SignalCard, SignalState, TradingMode,
    )
    return SignalCard(
        id=f"SC-{NOW}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_PE, direction=Direction.PE, state=SignalState.ACTIVE,
        contract="NIFTY 24500 PE", strike=24500.0, token=777, expiry="2026-08-07",
        entry_low=99.0, entry_high=102.0, premium_sl=82.0, target1=127.0,
        target2=145.0, trailing_sl_rule="r", risk_reward=1.5, confidence=82.0,
        underlying_invalidation="u", invalidation_note="n", lot_size=65,
        created_at=NOW, valid_until=NOW + 480, ref_entry_premium=entry,
        score=ScoreBreakdown(direction=Direction.PE, components=[], total=82.0),
    )


def test_twin_moves_only_the_stop():
    """THE CONFOUND GUARD: price_ladder derives T1/T2 from sl_pct, so a twin
    created with the calibrated sl_pct would move the targets too. The twin is
    created on the STATIC ladder and re-based via set_stop — same entry, same
    targets, different stop, in both directions (looser AND tighter)."""
    from app.trades.store import TradeStore

    with tempfile.TemporaryDirectory() as td:
        store = TradeStore(path=Path(td) / ".trades.json")
        static_sl, fill = 0.18, 100.0
        clean = store.create_from_signal(_card(), 1, fill, 65,
                                         sl_pct=static_sl, rr1=1.5, rr2=2.5)
        for calib in (0.12, 0.22):   # tighter than static, and looser
            twin = store.create_from_signal(
                _card(), 1, fill, 65, sl_pct=static_sl, rr1=1.5, rr2=2.5,
                notes=f"hollow: stopc: calibrated {calib:.0%} vs static 18%")
            store.set_stop(twin.id, round(fill * (1 - calib), 2), "calibrated")
            got = next(t for t in store.all() if t.id == twin.id)
            assert got.stop_loss == round(fill * (1 - calib), 2), got.stop_loss
            assert got.trailing_sl == got.stop_loss, "trailing must re-base too"
            assert got.target1 == clean.target1 and got.target2 == clean.target2, \
                "targets must NOT move — that would confound the A/B"
            assert got.entry_premium == clean.entry_premium
    print("  TWIN   -> only the stop moves; targets identical in both directions")


def test_twin_pauses_under_underlying_primary():
    """Review catch (05-Aug): under underlying-primary the monitor's disaster
    backstop decides exits and never consults stop_loss — both arms would
    exit identically and the ledger would grade noise. The twin books ONLY
    under premium-primary semantics, always with premium-stop semantics."""
    from app.config import Settings
    from app.paper.service import PaperTradingService, shadow_class
    from app.signals import calibration as cal
    from app.state import MarketState
    from app.trades.store import TradeStore

    orig = cal.intraday_sl_pct
    cal.intraday_sl_pct = lambda cfg: 0.12       # bypass the 30-fill gate
    try:
        def _svc(td, **over):
            store = TradeStore(path=Path(td) / "p.json")
            cfg = Settings(_env_file=None, SIGNAL_MAX_PREMIUM_AGE_S=0,
                           STOP_AB_PAIRED=False, **over)
            state = MarketState()
            state.ticks[777] = {"last_price": 100.0}
            return PaperTradingService(cfg, state, store), store

        with tempfile.TemporaryDirectory() as td:
            svc, store = _svc(td, STOP_PRIMARY="premium", TRADING_CAPITAL=500000)
            svc.consider(_card())
            twins = [t for t in store.all() if shadow_class(t) == "stopc"]
            assert len(twins) == 1, [t.notes for t in store.all()]
            clean = next(t for t in store.all() if shadow_class(t) is None)
            assert twins[0].disaster_sl is None, "twin must run premium semantics"
            assert twins[0].stop_loss == round(clean.entry_premium * 0.88, 2)

        with tempfile.TemporaryDirectory() as td:
            svc, store = _svc(td, STOP_PRIMARY="underlying", TRADING_CAPITAL=500000)
            svc.consider(_card())
            assert [t for t in store.all() if shadow_class(t) == "stopc"] == [], \
                "ledger must PAUSE under underlying-primary, not grade noise"
    finally:
        cal.intraday_sl_pct = orig
    print("  PAUSE  -> twin books under premium-primary only, premium semantics")


def test_stop_calib_ledger_pairs_and_stays_pure():
    from app.paper.service import shadow_class, summarize
    from app.trades.store import TradeStore

    assert shadow_class(SimpleNamespace(notes="hollow: stopc: x")) == "stopc"

    with tempfile.TemporaryDirectory() as td:
        store = TradeStore(path=Path(td) / ".trades.json")
        card = _card()
        clean = store.create_from_signal(card, 1, 100.0, 65,
                                         sl_pct=0.18, rr1=1.5, rr2=2.5)
        twin = store.create_from_signal(
            card, 1, 100.0, 65, sl_pct=0.18, rr1=1.5, rr2=2.5,
            notes="hollow: stopc: calibrated 12% vs static 18% — twin")
        store.set_stop(twin.id, 88.0, "calibrated")
        # Diverge them: static stop at 82 holds and the trade targets out;
        # the tighter calibrated stop at 88 is hit first.
        store.auto_close(clean.id, 112.0, "quick_target")
        store.auto_close(twin.id, 88.0, "stop_loss_hit")

        s = summarize(store)
        sc = s["stop_calib"]
        assert sc is not None and sc["n"] == 1 and sc["n_diverged"] == 1, sc
        assert sc["static_stop"]["net_pnl"] > sc["calibrated_stop"]["net_pnl"], sc
        # PURITY: the twin must not leak into the clean book, the floor's
        # verdict, or the headline counts.
        assert s["trades"] == 1, s["trades"]
        assert s["hollow"] is None, "stopc twin polluted the floor ledger"
        twin_rows = [r for r in s["rows"] if r["shadow_class"] == "stopc"]
        assert len(twin_rows) == 1
    print("  PAIR   -> ledger pairs by signal, diverges on exits, pollutes nothing")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
