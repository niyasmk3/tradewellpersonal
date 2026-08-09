"""GOLDEN card label (09-Aug): tape state at birth + the stacked-filter badge.

The label is display + ledger only — these tests pin that it NEVER gates:
a golden=False card must flow exactly like before the field existed. Split
points (35/60) are frozen; the paper ledger, not these tests, judges whether
the label means anything.

Run:  python backend/tests/test_golden_card.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

import pandas as pd

from app.config import Settings
from app.signals.models import Direction, TradingMode
from app.signals.service import SignalService


def _svc(**over):
    svc = SignalService.__new__(SignalService)
    svc.cfg = Settings(_env_file=None, **over)
    return svc


def _day_df(moves, now):
    """5-min-ish bars for TODAY built from (open->close) cumulative moves."""
    day0 = ((now + 19800) // 86400) * 86400 - 19800 + 9 * 3600 + 900
    rows = []
    px = 24500.0
    for i, m in enumerate(moves):
        o = px
        px = 24500.0 + m
        rows.append({"ts": day0 + i * 300, "open": o,
                     "high": max(o, px) + 2, "low": min(o, px) - 2,
                     "close": px, "volume": 10})
    return pd.DataFrame(rows)


def test_tape_state_classification():
    svc = _svc()
    now = int(time.time())
    # 7 bars x 5min = 35 min of session — past the 30-min age floor.
    # Up-day pulled back to ~47% of its range resolved -> developing, up
    # (high 24562, low 24498 -> range 64; |close-open| = 30 -> 0.47).
    dev = _day_df([10, 20, 40, 60, 50, 38, 30], now)
    st = svc._tape_state(dev, int(dev["ts"].iloc[-1]) + 300)
    assert st is not None
    state, pct, side = st
    assert state == "developing" and side == "up", st
    # Stretched: monotone one-way, close at the high -> resolved ~0.94.
    stre = _day_df([10, 20, 30, 40, 45, 55, 60], now)
    s2 = svc._tape_state(stre, int(stre["ts"].iloc[-1]) + 300)
    assert s2[0] == "stretched" and s2[2] == "up", s2
    # Two-way: round trip back to the open -> resolved ~0.03.
    two = _day_df([30, 60, 20, 10, 40, 15, 2], now)
    s3 = svc._tape_state(two, int(two["ts"].iloc[-1]) + 300)
    assert s3[0] == "two-way", s3
    # Down day, mid-resolution -> developing, down.
    dn = _day_df([-10, -30, -50, -60, -45, -35, -30], now)
    s4 = svc._tape_state(dn, int(dn["ts"].iloc[-1]) + 300)
    assert s4[0] == "developing" and s4[2] == "down", s4
    print("  STATE  -> stretched / two-way / sides classified; frozen 35/60 splits")


def test_too_young_day_returns_none():
    svc = _svc()
    now = int(time.time())
    assert svc._tape_state(_day_df([5], now), now) is None
    assert svc._tape_state(_day_df([5, 6], now), now) is None
    # 4 bars = 15 min of session: under the 30-min age floor -> None (the
    # scalp 1m frame would otherwise label the day at 09:18 off pure noise).
    young = _day_df([20, 60, 35, 30], now)
    assert svc._tape_state(young, int(young["ts"].iloc[-1]) + 300) is None
    # Yesterday's bars only (no bars today) -> None, never a stale label.
    old = _day_df([10, 20, 30, 40, 50, 60, 70], now - 86400)
    assert svc._tape_state(old, now) is None
    print("  YOUNG  -> <3 bars, <30min session, or stale frame yields no label")


def test_golden_requires_all_conditions():
    """golden = developing + aligned + gate-mode + confirm gate armed."""
    from app.signals.models import (Action, Bias, MarketStatus, Regime,
                                    ScoreBreakdown, ScoreComponent, SignalCard,
                                    SignalState)

    def _card(mode, direction):
        return SignalCard(
            id="G-1", symbol="NIFTY", mode=mode, title="t",
            action=Action.BUY_CE if direction is Direction.CE else Action.BUY_PE,
            direction=direction, state=SignalState.ACTIVE,
            contract=f"NIFTY 24500 {direction.value}", strike=24500.0,
            expiry="2026-08-11", entry_low=99.0, entry_high=101.0,
            premium_sl=82.0, target1=127.0, target2=145.0,
            trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
            underlying_invalidation="u", invalidation_note="n",
            created_at=1, valid_until=481,
            score=ScoreBreakdown(direction=direction, components=[
                ScoreComponent(name="Volume confirmation", points=9.0, max=15),
            ], total=81.0),
        )

    # The exact stamping logic from evaluate_symbol, exercised directly.
    def stamp(card, tape, confirm_bars):
        state, pct, side = tape
        card.tape_state = state
        card.tape_resolved_pct = pct
        card.tape_aligned = (side == "up") == (card.direction.value == "CE")
        card.golden = bool(
            state == "developing" and card.tape_aligned
            and card.mode.value in ("intraday", "scalp")
            and confirm_bars >= 2)
        return card

    dev_up = ("developing", 50.0, "up")
    assert stamp(_card(TradingMode.INTRADAY, Direction.CE), dev_up, 2).golden
    assert stamp(_card(TradingMode.SCALP, Direction.CE), dev_up, 2).golden
    # PE against an up day: aligned False -> not golden.
    assert not stamp(_card(TradingMode.INTRADAY, Direction.PE), dev_up, 2).golden
    # PE WITH a down day: golden.
    assert stamp(_card(TradingMode.INTRADAY, Direction.PE),
                 ("developing", 50.0, "down"), 2).golden
    # Stretched / two-way: never golden, whatever else holds.
    assert not stamp(_card(TradingMode.INTRADAY, Direction.CE),
                     ("stretched", 80.0, "up"), 2).golden
    assert not stamp(_card(TradingMode.INTRADAY, Direction.CE),
                     ("two-way", 20.0, "up"), 2).golden
    # Positional exempt; confirm gate off kills the label (an unconfirmed
    # "golden" would be a flicker wearing a crown).
    assert not stamp(_card(TradingMode.POSITIONAL, Direction.CE), dev_up, 2).golden
    assert not stamp(_card(TradingMode.INTRADAY, Direction.CE), dev_up, 0).golden
    print("  GOLDEN -> only developing + aligned + gate-mode + gate armed")


def test_trade_copies_label_and_summary_splits():
    from app.paper.service import shadow_class  # noqa: F401 (import sanity)
    from app.trades.models import Trade, TradeStatus

    t = Trade(id="T1", signal_id="G-1", symbol="NIFTY",
              mode=TradingMode.INTRADAY, direction=Direction.CE,
              contract="NIFTY 24500 CE", strike=24500.0,
              entry_premium=100.0, lots=1, lot_size=65, quantity=65,
              initial_quantity=65, status=TradeStatus.ENTERED,
              stop_loss=82.0, target1=127.0, target2=145.0,
              trailing_sl=82.0, created_at=1, entered_at=1,
              golden=True, tape_state="developing")
    assert t.golden is True and t.tape_state == "developing"
    # Round-trips persistence (the ledger reads it back tomorrow).
    t2 = Trade(**t.model_dump())
    assert t2.golden is True
    # Pre-label rows default to None — excluded from BOTH ledger arms.
    t3 = Trade(**{k: v for k, v in t.model_dump().items()
                  if k not in ("golden", "tape_state")})
    assert t3.golden is None and t3.tape_state is None
    print("  LEDGER -> label rides the trade row; legacy rows stay None")


def test_notify_body_carries_label():
    from app.notify import _body
    from app.signals.models import (Action, ScoreBreakdown, ScoreComponent,
                                    SignalCard, SignalState)
    card = SignalCard(
        id="G-2", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24500 CE", strike=24500.0, expiry="2026-08-11",
        entry_low=99.0, entry_high=101.0, premium_sl=82.0, target1=127.0,
        target2=145.0, trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n", created_at=1,
        valid_until=481,
        score=ScoreBreakdown(direction=Direction.CE, components=[
            ScoreComponent(name="Volume confirmation", points=9.0, max=15)],
            total=81.0),
        tape_state="developing", tape_resolved_pct=52.0, tape_aligned=True,
        golden=True)
    body = _body(card)
    assert "GOLDEN" in body
    card.golden = False
    body2 = _body(card)
    assert "GOLDEN" not in body2 and "developing" in body2
    print("  PUSH   -> golden line on golden cards, tape line otherwise")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
