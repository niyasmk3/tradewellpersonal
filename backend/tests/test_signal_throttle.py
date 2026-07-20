"""Signal-throttle + circuit-breaker tests.

Added 2026-07-20 after the engine issued ELEVEN cards in under three hours
(design goal: 1-4/day) and the trader followed them into a large loss. A
tradeable score is necessary but not sufficient — these gates decide how often
the engine is allowed to speak at all.

Run:  python backend/tests/test_signal_throttle.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

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
from app.signals.store import RiskState, SignalStore, ThrottleConfig

BASE = int(time.time()) // 86400 * 86400 + 5 * 3600   # a stable mid-day epoch
CFG = ThrottleConfig(max_per_day=4, min_gap_s=900, cooldown_s=600,
                     flip_guard_s=1800, max_consecutive_losses=2, daily_loss_limit=20000)


def _card(direction=Direction.PE, at=BASE, valid=480, cid="C"):
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_PE if direction is Direction.PE else Action.BUY_CE,
        direction=direction, state=SignalState.ACTIVE,
        contract=f"NIFTY 24200 {direction.value}", strike=24200.0, expiry="2026-07-21",
        entry_low=90.0, entry_high=94.0, premium_sl=75.0, target1=116.0, target2=133.0,
        trailing_sl_rule="r", risk_reward=1.5, confidence=80.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + valid,
        score=ScoreBreakdown(direction=direction, components=[], total=80.0, max=100),
    )


def _resp(card, at=BASE):
    bias = Bias.BEARISH if card and card.direction is Direction.PE else Bias.BULLISH
    return SignalResponse(
        symbol="NIFTY", mode=TradingMode.INTRADAY, evaluated_at=at,
        status=MarketStatus(
            symbol="NIFTY", mode=TradingMode.INTRADAY, regime=Regime.MODERATE_BEARISH,
            regime_label="R", bias=bias, bull_score=50, bear_score=50, headline="h",
            vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=card.action if card else Action.AVOID,
        signal=card, no_trade_reason=None, score=None,
    )


def _store():
    return SignalStore(store_path=None)     # memory-only; never touches .signals.json


def test_daily_cap():
    s = _store()
    t = BASE
    issued = 0
    for i in range(10):
        t += 1000                            # past min_gap and cooldown each time
        r = s.reconcile(_resp(_card(at=t, cid=f"c{i}"), t), t, CFG, RiskState())
        if r.signal:
            issued += 1
            t += 500                         # let it expire before the next attempt
            s.reconcile(_resp(None, t), t, CFG, RiskState())
    assert issued == CFG.max_per_day, issued
    print(f"  CAP    -> stopped at {issued}/day (was unbounded)")


def test_min_gap_between_signals():
    s = _store()
    r1 = s.reconcile(_resp(_card(at=BASE), BASE), BASE, CFG, RiskState())
    assert r1.signal is not None
    t = BASE + 500                            # card expired (480s) but gap < 900s
    s.reconcile(_resp(None, t), t, CFG, RiskState())
    r2 = s.reconcile(_resp(_card(at=t, cid="b"), t), t, CFG, RiskState())
    assert r2.signal is None
    assert "ooling down" in r2.no_trade_reason or "hrottled" in r2.no_trade_reason, r2.no_trade_reason
    print(f"  GAP    -> second signal withheld: {r2.no_trade_reason}")


def test_direction_flip_blocked():
    """Six PE cards then five CE cards inside 25 minutes is whipsaw, not edge."""
    s = _store()
    s.reconcile(_resp(_card(Direction.PE, BASE), BASE), BASE, CFG, RiskState())
    # Retire the PE card, then step past BOTH the cooldown (600s from retire)
    # and the min-gap (900s from issue) so the FLIP guard is what bites — it
    # runs last, and only the flip should still be blocking at +1200s.
    retire_at = BASE + 500
    s.reconcile(_resp(None, retire_at), retire_at, CFG, RiskState())
    t = BASE + 1200                           # >600 since retire, >900 since issue, <1800 flip guard
    r = s.reconcile(_resp(_card(Direction.CE, t, cid="ce"), t), t, CFG, RiskState())
    assert r.signal is None and "flip" in r.no_trade_reason.lower(), r.no_trade_reason
    print(f"  FLIP   -> {r.no_trade_reason}")


def test_consecutive_loss_breaker():
    s = _store()
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, CFG,
                    RiskState(consecutive_losses=2, realized_today=-5000))
    assert r.signal is None and "Circuit breaker" in r.no_trade_reason
    print(f"  LOSSES -> {r.no_trade_reason}")


def test_daily_loss_limit_breaker():
    s = _store()
    r = s.reconcile(_resp(_card(at=BASE), BASE), BASE, CFG,
                    RiskState(consecutive_losses=0, realized_today=-25000))
    assert r.signal is None and "loss limit" in r.no_trade_reason
    print(f"  LIMIT  -> {r.no_trade_reason}")


def test_counters_reset_next_day():
    s = _store()
    t = BASE
    for i in range(CFG.max_per_day):
        s.reconcile(_resp(_card(at=t, cid=f"d{i}"), t), t, CFG, RiskState())
        t += 500
        s.reconcile(_resp(None, t), t, CFG, RiskState())
        t += 1000
    blocked = s.reconcile(_resp(_card(at=t, cid="x"), t), t, CFG, RiskState())
    assert blocked.signal is None
    nxt = t + 86400                           # next IST day
    ok = s.reconcile(_resp(_card(at=nxt, cid="y"), nxt), nxt, CFG, RiskState())
    assert ok.signal is not None, "counters must reset with the IST day"
    print("  ROLL   -> capped today, free again tomorrow")


def test_throttle_never_blocks_an_already_active_card():
    """A live card must keep being served even once the cap is reached —
    otherwise a position's own signal vanishes from the UI."""
    s = _store()
    card = _card(at=BASE, valid=3600)
    s.reconcile(_resp(card, BASE), BASE, CFG, RiskState())
    t = BASE + 60
    r = s.reconcile(_resp(card, t), t, CFG, RiskState(consecutive_losses=9))
    assert r.signal is not None and r.signal.id == card.id
    print("  ACTIVE -> live card survives circuit breaker")


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
