"""Condor regime classifier + breakout risk on synthetic, deterministic tapes."""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.condor import regime as cregime

IST = timezone(timedelta(hours=5, minutes=30))
# A known 2026 trading Tuesday, 09:15 IST session open.
OPEN_TS = int(datetime(2026, 8, 11, 9, 15, tzinfo=IST).timestamp())
PREV_OPEN_TS = int(datetime(2026, 8, 10, 9, 15, tzinfo=IST).timestamp())


def _bars(open_ts: int, n: int, secs: int, closes, spread: float = 6.0,
          volume: float = 100_000.0) -> list[dict]:
    rows = []
    for i in range(n):
        c = closes(i)
        rows.append({
            "ts": open_ts + i * secs,
            "open": c, "high": c + spread, "low": c - spread,
            "close": c, "volume": volume,
        })
    return rows


def _range_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Gentle oscillation around 25000; session extremes set early."""
    def osc(i: int) -> float:
        return 25000 + 35 * math.sin(i / 2.5)

    rows5 = _bars(OPEN_TS, 4, 300, lambda i: 25000 + (80 if i == 1 else -80 if i == 2 else 0),
                  spread=15)
    rows5 += _bars(OPEN_TS + 4 * 300, 40, 300, lambda i: osc(i + 4))
    df5 = pd.DataFrame(rows5)

    # Fast oscillation (period ~5.7 bars): a slow sine reads as a trending
    # half-cycle to EMA/structure/RSI — which is correct behavior, so the
    # range fixture must genuinely chop.
    rows15 = _bars(PREV_OPEN_TS, 25, 900, lambda i: 25000 + 25 * math.sin(i * 1.1))
    rows15 += _bars(OPEN_TS, 11, 900, lambda i: 25000 + 25 * math.sin((i + 25) * 1.1))
    df15 = pd.DataFrame(rows15)
    return df5, df15


def _trend_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Accelerating breakout with a volume ramp — ADX building, ATR expanding."""
    df5 = pd.DataFrame(_bars(OPEN_TS, 44, 300, lambda i: 24800 + 0.55 * i * i))
    df5["volume"] = [100_000 + 6_000 * i for i in range(len(df5))]
    rows15 = _bars(PREV_OPEN_TS, 25, 900, lambda i: 24500 + i * 8)
    rows15 += _bars(OPEN_TS, 11, 900, lambda i: 24700 + 2.2 * i * i)
    return df5, pd.DataFrame(rows15)


def test_range_tape_qualifies():
    df5, df15 = _range_frames()
    out = cregime.classify(df5, df15, prev_close=25005, vix_status="Stable",
                           vix_change_pct=0.5, range_vs_typical=90,
                           gap_ended_at=None)
    assert not out.vetoes
    assert out.label == cregime.RANGE_BOUND, (out.label, out.votes, out.notes)
    assert out.condor_allowed
    assert out.confidence > 0
    assert out.bars_held >= 3


def test_trend_tape_blocked():
    df5, df15 = _trend_frames()
    out = cregime.classify(df5, df15, prev_close=24500, vix_status="Stable",
                           vix_change_pct=0.0, range_vs_typical=140,
                           gap_ended_at=None)
    assert out.label in (cregime.TRENDING, cregime.NOT_QUALIFIED, cregime.VOLATILE)
    assert not out.condor_allowed


def test_atr_spike_vetoes():
    df5, df15 = _range_frames()
    # One violent 5m bar at the end: range ~15x the quiet bars.
    df5.loc[df5.index[-1], "high"] = 25400.0
    df5.loc[df5.index[-1], "low"] = 24800.0
    out = cregime.classify(df5, df15, prev_close=25005, vix_status="Stable",
                           vix_change_pct=0.0, range_vs_typical=90,
                           gap_ended_at=None)
    assert out.label == cregime.VOLATILE
    assert any("ATR spike" in v for v in out.vetoes)
    assert not out.condor_allowed


def test_vix_spike_vetoes():
    df5, df15 = _range_frames()
    out = cregime.classify(df5, df15, prev_close=25005, vix_status="Elevated",
                           vix_change_pct=6.2, range_vs_typical=90,
                           gap_ended_at=None)
    assert out.label == cregime.VOLATILE
    assert not out.condor_allowed


def test_warming_up_short_frames():
    df5, df15 = _range_frames()
    out = cregime.classify(df5.head(5), df15.head(5), prev_close=None,
                           vix_status=None, vix_change_pct=None,
                           range_vs_typical=None, gap_ended_at=None)
    assert out.label == cregime.WARMING_UP


def test_vwap_crossings_counts_two_way_tape():
    df5, _ = _range_frames()
    n = cregime.vwap_crossings(df5, atr5=20.0)
    assert n >= 4


def test_breakout_risk_low_on_quiet_range_high_on_trend():
    df5, df15 = _range_frames()
    quiet = cregime.breakout_risk(df5, df15, vix_change_pct=0.0)
    tdf5, tdf15 = _trend_frames()
    loud = cregime.breakout_risk(tdf5, tdf15, vix_change_pct=5.0)
    assert quiet.score < loud.score
    assert quiet.band in ("LOW", "MEDIUM")
    assert loud.band in ("HIGH", "EXTREME"), (loud.score, loud.parts)


def test_oi_unwinding_adds_points():
    df5, df15 = _range_frames()
    base = cregime.breakout_risk(df5, df15, vix_change_pct=0.0)
    shifted = cregime.breakout_risk(df5, df15, vix_change_pct=0.0,
                                    oi_shift_against=True)
    assert shifted.score == base.score + 10
