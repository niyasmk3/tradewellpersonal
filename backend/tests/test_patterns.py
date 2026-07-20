"""Candlestick-pattern detector tests.

    python tests/test_patterns.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from app.signals import patterns


def _df(rows: list[dict]) -> pd.DataFrame:
    # 18 small neutral filler candles (range ~2) so the rolling reference is ~2,
    # then the pattern candles as the final rows.
    filler = [{"open": 100, "high": 101, "low": 99, "close": 100.2, "volume": 1000} for _ in range(18)]
    return pd.DataFrame(filler + rows)


def names(df) -> list[str]:
    return [p.name for p in patterns.detect(df)]


def test_hammer():
    df = _df([{"open": 100, "high": 101.2, "low": 95, "close": 101, "volume": 1000}])
    assert "Hammer" in names(df), names(df)
    print("  Hammer ✓")


def test_shooting_star():
    df = _df([{"open": 100, "high": 105, "low": 98.8, "close": 99, "volume": 1000}])
    assert "Shooting Star" in names(df), names(df)
    print("  Shooting Star ✓")


def test_doji():
    df = _df([{"open": 100, "high": 102, "low": 98, "close": 100.1, "volume": 1000}])
    assert "Doji" in names(df), names(df)
    print("  Doji ✓")


def test_bullish_marubozu():
    df = _df([{"open": 100, "high": 106.05, "low": 99.98, "close": 106, "volume": 1000}])
    assert "Bullish Marubozu" in names(df), names(df)
    print("  Bullish Marubozu ✓")


def test_bullish_engulfing():
    df = _df([
        {"open": 103, "high": 103.5, "low": 100.5, "close": 101, "volume": 1000},  # bearish
        {"open": 100.5, "high": 104, "low": 100, "close": 103.5, "volume": 1000},  # engulfs
    ])
    assert "Bullish Engulfing" in names(df), names(df)
    print("  Bullish Engulfing ✓")


def test_bearish_engulfing():
    df = _df([
        {"open": 100, "high": 102.5, "low": 99.5, "close": 102, "volume": 1000},   # bullish
        {"open": 102.5, "high": 103, "low": 99, "close": 99.5, "volume": 1000},    # engulfs down
    ])
    assert "Bearish Engulfing" in names(df), names(df)
    print("  Bearish Engulfing ✓")


def test_morning_star():
    df = _df([
        {"open": 110, "high": 110.2, "low": 103.8, "close": 104, "volume": 1000},  # long bearish
        {"open": 103.5, "high": 104, "low": 102.8, "close": 103, "volume": 1000},  # small star
        {"open": 104, "high": 109, "low": 103, "close": 108, "volume": 1000},      # long bullish, closes > mid
    ])
    assert "Morning Star" in names(df), names(df)
    print("  Morning Star ✓")


def test_confirmation_direction_and_conflict():
    hammer = _df([{"open": 100, "high": 101.2, "low": 95, "close": 101, "volume": 1000}])
    # Bullish pattern confirms a bullish (CE) candidate.
    up_delta, up_reasons = patterns.confirmation(hammer, bullish=True, near_vwap=True)
    assert up_delta > 0 and up_reasons, (up_delta, up_reasons)
    # Same bullish pattern is a caution for a bearish (PE) candidate.
    dn_delta, _ = patterns.confirmation(hammer, bullish=False, near_vwap=False)
    assert dn_delta < 0, dn_delta
    # An ordinary candle (mid-size body, small wicks) matches no pattern -> no effect.
    flat = _df([{"open": 100, "high": 100.8, "low": 99.9, "close": 100.6, "volume": 1000}])
    assert patterns.detect(flat) == [], patterns.detect(flat)
    assert patterns.confirmation(flat, bullish=True, near_vwap=False) == (0.0, [])
    print(f"  Confirmation ✓ (bull +{up_delta} at VWAP, conflict {dn_delta})")


def _main():
    tests = [
        test_hammer, test_shooting_star, test_doji, test_bullish_marubozu,
        test_bullish_engulfing, test_bearish_engulfing, test_morning_star,
        test_confirmation_direction_and_conflict,
    ]
    failed = 0
    for t in tests:
        try:
            print(f"• {t.__name__}"); t()
        except AssertionError as e:
            failed += 1; print(f"  FAIL: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  ERROR: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
