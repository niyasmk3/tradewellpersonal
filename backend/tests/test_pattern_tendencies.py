"""Tendencies layer of the Patterns Module: volume-conditioned pattern
outcomes and the live tape read.

The layer's whole value is honesty discipline — cells under n=10 vanish,
nothing under n=30 may be called a tendency, 45-55% is "coin" at any n — so
the tests exercise exactly those rules on synthetic frames engineered to
land on both sides of each boundary.

Run:  python backend/tests/test_pattern_tendencies.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

import numpy as np
import pandas as pd

from app.patterns.tendencies import (
    HORIZONS,
    assemble_live_read,
    conditional_outcomes,
    volume_pace_curve,
    volume_regime,
)

IST_OFFSET = 19800


def _session_ts(day: int, bar: int) -> int:
    """Epoch for 5-min bar `bar` of synthetic session `day` (09:15 IST open)."""
    base = (int(time.time()) + IST_OFFSET) // 86400 * 86400 - IST_OFFSET
    return base - day * 86400 + 9 * 3600 + 15 * 60 + bar * 300


def _flat_day(day: int, bars: int = 75, px: float = 100.0, vol: float = 100.0):
    rows = []
    for b in range(bars):
        rows.append({"ts": _session_ts(day, b), "open": px, "high": px + 0.05,
                     "low": px - 0.05, "close": px, "vol_proxy": vol})
    return rows


def test_volume_regime_buckets_and_missing_proxy():
    vol = pd.Series([100.0] * 30 + [200.0, 60.0, 100.0, 0.0])
    reg = volume_regime(vol)
    assert reg.iloc[30] == "high"       # 2.0x the 100 median
    assert reg.iloc[31] == "low"        # 0.6x
    assert reg.iloc[32] == "normal"
    assert reg.iloc[33] is None, "absent proxy must not masquerade as a dead tape"
    print("  REGIME -> high/normal/low split; missing proxy stays None")


def _engineered_frame(n_events=40, up_after=True, event_vol=300.0):
    """Sessions each containing one textbook bull_marubozu whose 30m outcome
    is controlled: big bullish body on `event_vol` proxy volume, then six bars
    drifting up (or down) — a deterministic conditional table."""
    rows = []
    for day in range(n_events):
        px = 100.0
        day_rows = _flat_day(day, bars=30)
        # event bar at index 20: long bullish body (range ~= body >> flat bars)
        ev = day_rows[20]
        ev.update(open=px, low=px - 0.02, close=px + 3.0, high=px + 3.02,
                  vol_proxy=event_vol)
        drift = 1.0 if up_after else -1.0
        for k in range(1, 7):           # the 30m window after the event
            day_rows[20 + k].update(
                open=px + 3.0 + drift * 0.3 * (k - 1),
                close=px + 3.0 + drift * 0.3 * k,
                high=px + 3.0 + drift * 0.3 * k + 0.05,
                low=px + 3.0 + drift * 0.3 * (k - 1) - 0.05,
            )
        rows.extend(day_rows)
    df = pd.DataFrame(sorted(rows, key=lambda r: r["ts"]))
    return df.reset_index(drop=True)


def test_conditional_outcomes_scores_direction_and_volume():
    df = _engineered_frame(n_events=40, up_after=True, event_vol=300.0)
    table = conditional_outcomes(df)
    assert "bull_marubozu" in table, list(table)
    p = table["bull_marubozu"]
    assert p["direction"] == "bullish"
    h30 = p["horizons"]["30m"]
    assert h30["all"]["n"] >= 30 and h30["all"]["hit_rate"] == 1.0
    assert h30["all"]["verdict"] == "tendency"
    # 300 vs flat-100 median -> every event bar is a HIGH-volume cell.
    assert h30["by_volume"]["high"]["n"] >= 30
    assert h30["by_volume"]["high"]["hit_rate"] == 1.0
    # A losing variant scores 0.0 — the sign convention is the pattern's own.
    lose = conditional_outcomes(_engineered_frame(n_events=40, up_after=False))
    assert lose["bull_marubozu"]["horizons"]["30m"]["all"]["hit_rate"] == 0.0
    print("  TABLE  -> outcomes signed by textbook direction; vol cell splits")


def test_cell_and_tendency_floors():
    """n<10 cells vanish; 45-55%% is 'coin' regardless of n."""
    few = conditional_outcomes(_engineered_frame(n_events=8))
    assert "bull_marubozu" not in few, "under 10 events nothing may be scored"

    # 50/50 outcomes at n=40 -> coin, never tendency.
    up = _engineered_frame(n_events=20, up_after=True)
    down = _engineered_frame(n_events=20, up_after=False)
    down["ts"] = down["ts"] - 400 * 86400          # separate sessions
    mixed = pd.concat([down, up], ignore_index=True).sort_values("ts").reset_index(drop=True)
    t = conditional_outcomes(mixed)
    cell = t["bull_marubozu"]["horizons"]["30m"]["all"]
    assert cell["n"] >= 30 and abs(cell["hit_rate"] - 0.5) < 0.11
    assert cell["verdict"] == "coin"
    print("  FLOORS -> n<10 omitted; 50% at any n stays a coin")


def test_horizon_respects_session_boundary():
    """An event 3 bars before the close has no same-session 30m window — it
    must be excluded from the 30m cell, not scored against tomorrow's open."""
    rows = []
    for day in range(15):
        day_rows = _flat_day(day, bars=75)
        ev = day_rows[71]                # 3 bars left: 15m ok, 30m impossible
        ev.update(open=100.0, low=99.98, close=103.0, high=103.02, vol_proxy=300.0)
        rows.extend(day_rows)
    df = pd.DataFrame(sorted(rows, key=lambda r: r["ts"])).reset_index(drop=True)
    t = conditional_outcomes(df)
    horizons = (t.get("bull_marubozu") or {}).get("horizons", {})
    assert "30m" not in horizons, "cross-session windows must not be scored"
    assert "15m" in horizons and horizons["15m"]["all"]["n"] >= 10
    print("  WINDOW -> horizons never cross the overnight boundary")


def test_pace_curve_uses_full_proxy_sessions_only():
    rows = []
    for day in range(4):
        rows.extend(_flat_day(day, bars=75, vol=100.0))
    rows.extend(_flat_day(4, bars=75, vol=0.0))     # no proxy -> excluded
    rows.extend(_flat_day(5, bars=20, vol=100.0))   # partial day -> excluded
    df = pd.DataFrame(sorted(rows, key=lambda r: r["ts"])).reset_index(drop=True)
    pace = volume_pace_curve(df)
    assert pace["days"] == 4
    assert len(pace["cum_median"]) == 75
    assert pace["cum_median"][0] == 100.0 and pace["cum_median"][74] == 7500.0
    print("  PACE   -> curve from full proxy sessions; partial/blind days out")


def test_live_read_joins_the_matching_cell():
    """A high-volume bull_marubozu on today's tape must surface with the
    HIGH-volume historical cell attached (and the unconditioned row along)."""
    results = {
        "conditional_outcomes": {
            "bull_marubozu": {
                "direction": "bullish", "n_total": 500,
                "horizons": {"30m": {
                    "all": {"n": 500, "hit_rate": 0.5, "avg_bps": 0.1,
                            "median_bps": 0.0, "verdict": "coin"},
                    "by_volume": {"high": {"n": 120, "hit_rate": 0.58,
                                           "avg_bps": 2.0, "median_bps": 1.0,
                                           "verdict": "tendency"}},
                }},
            }
        },
        "volume_pace": {"days": 60, "cum_median": [100.0 * (i + 1) for i in range(75)]},
    }
    tail = pd.DataFrame(_flat_day(1, bars=75))
    today_rows = _flat_day(0, bars=24)
    today_rows[-1].update(open=100.0, low=99.98, close=103.0, high=103.02,
                          vol_proxy=300.0)
    today = pd.DataFrame(today_rows)

    read = assemble_live_read(today, tail, results)
    assert read["bars_today"] == 24
    hits = [p for p in read["patterns"] if p["pattern"] == "bull_marubozu"]
    assert hits, read["patterns"]
    p = hits[0]
    assert p["volume_regime"] == "high" and p["conditioned"]
    assert p["historical_30m"]["hit_rate"] == 0.58
    assert p["historical_30m_all"]["hit_rate"] == 0.5
    # Volume block: 24 flat bars at 100 + one 300 spike = 2600 cum vs 2400
    # typical -> pace ~1.08x; the 30m-vs-prior-30m trend rises on the spike
    # (review catch: the trend classification must be ASSERTED, not just
    # exercised — an inverted threshold passed silently before this line).
    assert read["volume"]["pace_vs_typical"] == 1.08
    assert read["volume"]["current_regime"] == "high"
    assert read["volume"]["trend"] == "rising"
    assert read["volume"]["trend_window_min"] == 30
    print("  LIVE   -> today's bar joins its volume-matched historical cell")


def test_exclude_current_day_drops_todays_bars():
    """The 'historical' table must not contain today's own realized outcomes
    (review catch: a mid-session re-analyze would otherwise grade the morning
    and serve it back as independent history the same afternoon)."""
    from app.patterns.tendencies import exclude_current_day

    rows = _flat_day(0, bars=20) + _flat_day(1, bars=75) + _flat_day(2, bars=75)
    df = pd.DataFrame(sorted(rows, key=lambda r: r["ts"])).reset_index(drop=True)
    hist = exclude_current_day(df)
    assert len(hist) == 150, f"today's 20 bars must drop, got {len(df) - len(hist)}"
    assert int(hist["ts"].max()) < _session_ts(0, 0)
    assert exclude_current_day(pd.DataFrame()).empty
    print("  TODAY  -> current session never enters its own reference table")


def test_live_read_empty_day():
    read = assemble_live_read(pd.DataFrame(), pd.DataFrame(_flat_day(1)), {})
    assert read["bars_today"] == 0 and read["patterns"] == []
    print("  EMPTY  -> pre-open read degrades gracefully")


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
