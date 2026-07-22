"""Signal re-price tests.

A refresh re-prices an active card against the current premium so a stale
positional signal becomes orderable. It changes what a later Kite hand-off
sends, so it must: use the same ladder maths as issue time, keep the trade's
identity, re-stamp freshness, and never touch the daily signal budget.

Run:  python backend/tests/test_reprice.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.signals import risk as risk_mod
from app.signals.models import (
    Action, Bias, Direction, MarketStatus, Regime, ScoreBreakdown,
    SignalCard, SignalResponse, SignalState, TradingMode,
)
from app.signals.store import SignalStore, ThrottleConfig

BASE = int(time.time()) // 86400 * 86400 + 5 * 3600


def _card(created, valid_span=6 * 3600, cid="C"):
    return SignalCard(
        id=f"{cid}-{created}", symbol="NIFTY", mode=TradingMode.POSITIONAL, title="t",
        action=Action.BUY_PE, direction=Direction.PE, state=SignalState.ACTIVE,
        contract="NIFTY 24150 PE", strike=24150.0, token=999, expiry="2026-07-28",
        entry_low=26.85, entry_high=27.65, premium_sl=22.20, disaster_sl=14.90,
        quick_target=30.35, target1=34.45, target2=39.35, trailing_sl_rule="r",
        risk_reward=1.5, confidence=80.0, underlying_invalidation="NIFTY below 24210",
        invalidation_note="n", invalidation_level=24210.4, invalidation_dir="below",
        created_at=created, valid_until=created + valid_span, ref_entry_premium=27.10,
        score=ScoreBreakdown(direction=Direction.PE, components=[], total=80.0, max=100),
    )


def _resp(card):
    return SignalResponse(
        symbol="NIFTY", mode=TradingMode.POSITIONAL, evaluated_at=card.created_at,
        status=MarketStatus(
            symbol="NIFTY", mode=TradingMode.POSITIONAL, regime=Regime.MODERATE_BEARISH,
            regime_label="R", bias=Bias.BEARISH, bull_score=20, bear_score=80,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=Action.BUY_PE, signal=card, no_trade_reason=None, score=None,
    )


def _store_with_active(card):
    s = SignalStore(store_path=None)
    s.reconcile(_resp(card), card.created_at,
                ThrottleConfig(min_gap_s=0, cooldown_s=0, flip_guard_s=0))
    return s


def test_reprice_recomputes_the_ladder_from_the_new_premium():
    """The whole point: entry zone and levels track the current premium."""
    old = _card(created=BASE)
    old_entry_high = old.entry_high                    # snapshot: the store mutates in place
    s = _store_with_active(old)
    new_ltp = 31.50                                    # premium ran up since issue
    ladder = risk_mod.price_ladder(new_ltp, 0.18, 2.0, 3.5, disaster_pct=0.45, quick_pct=0.12)
    now = BASE + 2 * 3600
    out = s.reprice_active("NIFTY", TradingMode.POSITIONAL, new_ltp, ladder, now)

    assert out is not None
    # Levels must equal the ladder computed from 31.50, NOT the old 27.10 values.
    assert out.entry_high == ladder["entry_high"] == round(round(31.50 * 1.02 / 0.05) * 0.05, 2)
    assert out.premium_sl == ladder["premium_sl"]
    assert out.target1 == ladder["target1"]
    assert out.ref_entry_premium == 31.50
    assert out.entry_high > old_entry_high             # tracked the premium up
    print(f"  REPRICE-> zone re-priced 27.10 -> 31.50: entry_high {old_entry_high} -> {out.entry_high}")


def test_reprice_restamps_freshness_but_keeps_identity():
    old = _card(created=BASE, cid="KEEP")
    s = _store_with_active(old)
    now = BASE + 3 * 3600
    span = old.valid_until - old.created_at
    ladder = risk_mod.price_ladder(30.0, 0.18, 2.0, 3.5)
    out = s.reprice_active("NIFTY", TradingMode.POSITIONAL, 30.0, ladder, now)

    assert out.id == old.id                             # same trade
    assert out.strike == old.strike and out.direction == old.direction
    assert out.invalidation_level == old.invalidation_level  # index level untouched
    assert out.created_at == now                        # re-stamped
    assert out.valid_until == now + span                # window from now
    print(f"  REPRICE-> same id/strike/invalidation; created_at re-stamped to now")


def test_reprice_does_not_consume_the_daily_signal_budget():
    """Refreshing a card you're already looking at is not a new signal."""
    old = _card(created=BASE)
    s = _store_with_active(old)
    slot_before = s._slots[f"NIFTY:{TradingMode.POSITIONAL.value}"].issued
    ladder = risk_mod.price_ladder(30.0, 0.18, 2.0, 3.5)
    s.reprice_active("NIFTY", TradingMode.POSITIONAL, 30.0, ladder, BASE + 1000)
    slot_after = s._slots[f"NIFTY:{TradingMode.POSITIONAL.value}"].issued
    assert slot_before == slot_after == 1, (slot_before, slot_after)
    print("  REPRICE-> daily signal count unchanged by a refresh")


def test_reprice_no_active_card_returns_none():
    s = SignalStore(store_path=None)
    ladder = risk_mod.price_ladder(30.0, 0.18, 2.0, 3.5)
    assert s.reprice_active("NIFTY", TradingMode.POSITIONAL, 30.0, ladder, BASE) is None
    print("  REPRICE-> no active card -> None (route turns this into a 409)")


def test_reprice_latest_reflects_the_change():
    """The GET /signals view must show the refreshed levels immediately."""
    old = _card(created=BASE)
    s = _store_with_active(old)
    ladder = risk_mod.price_ladder(31.50, 0.18, 2.0, 3.5)
    now = BASE + 7200
    s.reprice_active("NIFTY", TradingMode.POSITIONAL, 31.50, ladder, now)
    latest = s.latest("NIFTY", TradingMode.POSITIONAL)
    assert latest.signal.entry_high == ladder["entry_high"]
    assert latest.evaluated_at == now
    print("  REPRICE-> latest() serves the refreshed card and a fresh evaluated_at")


def test_ladder_matches_engine_build_exactly():
    """price_ladder is the same maths build() uses — a drift here would make a
    refreshed card price differently from a freshly issued one."""
    import pandas as pd
    df = pd.DataFrame({"open": [24000.0] * 3, "high": [24010.0] * 3,
                       "low": [23990.0] * 3, "close": [24000.0] * 3})
    ind = type("I", (), {"vwap": None})()
    plan = risk_mod.build(Direction.PE, 27.10, df, ind, 24000.0, "NIFTY", "3m",
                          0.18, 2.0, 3.5, disaster_pct=0.45, quick_pct=0.12)
    ladder = risk_mod.price_ladder(27.10, 0.18, 2.0, 3.5, disaster_pct=0.45, quick_pct=0.12)
    for k in ("entry_low", "entry_high", "premium_sl", "disaster_sl", "quick_target",
              "target1", "target2"):
        assert getattr(plan, k) == ladder[k], (k, getattr(plan, k), ladder[k])
    print("  LADDER -> refresh ladder == engine build, field for field")


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
