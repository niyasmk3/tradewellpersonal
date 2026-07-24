"""Volatility PRICE (VIX percentile) — the IV-rank idea at the index level.

The score knew the VIX *level* but not whether premium was rich or cheap
against its own year. These pin the percentile math, the score adjustment, and
the graceful degradation when history is absent (the pre-24-Jul behaviour).

Run:  python backend/tests/test_vol_price.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.schemas import IndicatorSnapshot
from app.signals.scoring import _volatility
from app.state import MarketState

IND = IndicatorSnapshot(atr=14.0)          # +2 data credit in the component
NO_ATR = IndicatorSnapshot()


def _state_with(closes, current=None, token=99):
    s = MarketState()
    s.vix_token = token
    s.vix_daily_closes = closes
    if current is not None:
        s.ticks[token] = {"last_price": current}
    return s


def test_percentile_math():
    closes = [10.0 + i * 0.1 for i in range(252)]      # 10.0 .. 35.1
    s = _state_with(closes)
    assert s.vix_percentile(10.05) == round(100 * 1 / 252, 1)
    assert s.vix_percentile(40.0) == 100.0
    assert s.vix_percentile(9.0) == 0.0
    mid = s.vix_percentile(22.55)                       # ~half the closes below
    assert 48.0 <= mid <= 52.0, mid
    print(f"  VOLPX  -> percentile math: 1-in-252 low, {mid} mid, 100 high")


def test_percentile_needs_real_history_and_a_price():
    assert _state_with([12.0] * 59).vix_percentile(13.0) is None   # < 60 sessions
    assert _state_with([12.0] * 252).vix_percentile(None) is None  # no tick either
    s = _state_with([12.0] * 252, current=13.5)
    assert s.vix_percentile() is not None                           # falls back to tick
    print("  VOLPX  -> <60 sessions or no price → None (never a noise number)")


def test_rich_premium_is_penalised_and_cheap_rewarded():
    base = _volatility(IND, "Stable").points                 # no percentile: 8+2
    assert base == 10.0
    rich = _volatility(IND, "Stable", vix_percentile=91.0)
    assert rich.points == base - 3, rich.points
    assert any("rich" in r for r in rich.reasons)
    above = _volatility(IND, "Stable", vix_percentile=65.0)
    assert above.points == base - 1, above.points
    cheap = _volatility(NO_ATR, "Stable", vix_percentile=10.0)
    assert cheap.points == 8 + 1, cheap.points               # bonus, no ATR credit
    neutral = _volatility(IND, "Stable", vix_percentile=45.0)
    assert neutral.points == base
    assert any("pctl 45" in r for r in neutral.reasons)      # context always shown
    print("  VOLPX  -> pctl 91: −3 · 65: −1 · 45: 0 · 10: +1, reasons attached")


def test_component_stays_inside_its_budget():
    floor = _volatility(NO_ATR, "High", vix_percentile=95.0)  # 2 − 3 → clamps at 0
    assert floor.points == 0.0, floor.points
    ceil = _volatility(IND, "Calm", vix_percentile=5.0)       # 8 + 1 + 2 → clamps at 10
    assert ceil.points == 10.0, ceil.points
    print("  VOLPX  -> clamped to 0..10: never negative, never over budget")


def test_absent_history_keeps_the_old_behaviour():
    old = _volatility(IND, "Elevated")
    assert old.points == 7.0 and old.reasons == ["VIX Elevated"]
    print("  VOLPX  -> no percentile → exactly the pre-24-Jul component")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
