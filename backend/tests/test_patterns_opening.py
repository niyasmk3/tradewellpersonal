"""Opening-window study: session table correctness and honesty discipline.

The module's claims live or die on the session builder — a wrong gap sign, a
lookahead feature, or a partial morning slipping through would silently poison
every conditional cell downstream. So the tests engineer synthetic sessions
with KNOWN gaps, ranges, breakouts and fills, and assert the table reads them
back exactly.

Run:  python backend/tests/test_patterns_opening.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

import pandas as pd

from app.patterns.opening import (
    FIRST45_BARS,
    PACE_MIN_SESSIONS,
    PACE_SESSIONS,
    build_sessions,
    opening_study,
)

IST_OFFSET = 19800


def _session_ts(day: int, bar: int) -> int:
    base = (int(time.time()) + IST_OFFSET) // 86400 * 86400 - IST_OFFSET
    return base - day * 86400 + 9 * 3600 + 15 * 60 + bar * 300


def _flat_day(day: int, bars: int = 75, px: float = 100.0, vol: float = 100.0):
    rows = []
    for b in range(bars):
        rows.append({"ts": _session_ts(day, b), "open": px, "high": px + 0.05,
                     "low": px - 0.05, "close": px, "vol_proxy": vol})
    return rows


def _frame(rows):
    return pd.DataFrame(sorted(rows, key=lambda r: r["ts"])).reset_index(drop=True)


def test_gap_and_or_read_back_exactly():
    rows = _flat_day(2, px=100.0)                     # prior day closes 100
    day1 = _flat_day(1, px=101.0)                     # +1% gap up = +100 bps
    # widen bar 4 (inside OR30) so OR30 > OR15
    day1[4].update(high=101.6, low=100.8)
    rows += day1
    sess = build_sessions(_frame(rows))
    assert len(sess) == 2
    s = sess.iloc[1]
    assert s["gap_bps"] == 100.0 and s["gap_bucket"] == "big_up"
    assert s["or15_bps"] == round(0.10 / 101.0 * 1e4, 1)      # flat 0.05 bars
    assert abs(s["or30_bps"] - round(0.80 / 101.0 * 1e4, 1)) < 0.1
    print("  SESSIONS -> gap +100bps/big_up; OR15/OR30 read back exactly")


def test_partial_or_late_open_sessions_are_dropped():
    rows = _flat_day(3)                               # full, opens 09:15
    rows += _flat_day(2)[:40]                         # only 40 bars
    late = _flat_day(1)[3:]                           # opens 09:30
    rows += late
    sess = build_sessions(_frame(rows))
    assert len(sess) == 1, "partial and late-open sessions must not qualify"
    print("  SESSIONS -> 40-bar day and 09:30 open both dropped")


def test_gap_across_dropped_day_uses_that_days_close():
    rows = _flat_day(3, px=100.0)
    partial = _flat_day(2, px=110.0)[:30]             # dropped, but closes 110
    rows += partial
    rows += _flat_day(1, px=111.0)
    sess = build_sessions(_frame(rows))
    s = sess.iloc[-1]
    # gap must be vs the PARTIAL day's 110 close, not day 3's 100
    assert abs(s["gap_bps"] - round((111 / 110 - 1) * 1e4, 1)) < 0.2
    print("  SESSIONS -> gap spans a dropped partial day correctly")


def test_or30_breakout_direction_and_forward_return():
    rows = _flat_day(2, px=100.0)
    day = _flat_day(1, px=100.0)
    # break above OR30 (high 100.05) at bar 10, then drift up 0.1/bar
    for k in range(10, 30):
        px = 100.5 + 0.1 * (k - 10)
        day[k].update(open=px, close=px, high=px + 0.05, low=px - 0.05)
    rows += day
    sess = build_sessions(_frame(rows))
    s = sess.iloc[-1]
    assert s["brk_dir"] == 1 and s["brk_idx"] == 10
    expected = day[16]["close"] / day[10]["close"] - 1
    assert abs(s["brk_fwd30"] - expected) < 1e-9
    print("  BREAKOUT -> first close above OR30 at bar 10, fwd30 exact")


def test_gap_fill_is_directional_and_timed():
    rows = _flat_day(2, px=100.0)
    day = _flat_day(1, px=101.0)                      # gap up 100bps
    day[12].update(low=99.9)                          # touches 100 at bar 12
    rows += day
    sess = build_sessions(_frame(rows))
    s = sess.iloc[-1]
    assert s["fill_idx"] == 12
    # a gap-DOWN day whose lows go lower must NOT count as filled
    rows2 = _flat_day(2, px=100.0)
    d2 = _flat_day(1, px=99.0)
    d2[5].update(low=98.0)                            # falls further, no fill
    rows2 += d2
    s2 = build_sessions(_frame(rows2)).iloc[-1]
    assert pd.isna(s2["fill_idx"]), "gap-down fills UP through prior close only"
    print("  GAP FILL -> directional touch at bar 12; wrong-side touch ignored")


def test_no_lookahead_in_features():
    """Every feature must be identical whether or not the afternoon exists —
    computed from the first 45 minutes + prior day only. The afternoon-varied
    day gets a huge rally after 10:00; features must not move."""
    feature_cols = ["gap_bps", "gap_bucket", "or15_bps", "or30_bps", "or45_bps",
                    "f45_ret", "f45_dir", "above_vwap", "f45_vol", "prev_pos",
                    "pace"]
    rows_a = _flat_day(2, px=100.0) + _flat_day(1, px=100.5)
    day_b = _flat_day(1, px=100.5)
    # Moonshot starts at the FIRST bar outside the window (review catch: a
    # 20-bar start left 10:00-10:55 identical, blind to an off-by-one leak
    # like g.iloc[:FIRST45_BARS + 1]).
    for k in range(FIRST45_BARS, 75):
        day_b[k].update(open=120.0, high=125.0, low=119.0, close=124.0)
    rows_b = _flat_day(2, px=100.0) + day_b
    fa = build_sessions(_frame(rows_a)).iloc[-1]
    fb = build_sessions(_frame(rows_b)).iloc[-1]
    for c in feature_cols:
        assert fa[c] == fb[c] or (pd.isna(fa[c]) and pd.isna(fb[c])), c
    print("  NO-LOOKAHEAD -> all 10:00 features immune to the afternoon")


def test_pace_excludes_today_and_needs_history():
    """A volume RAMP makes the yardstick discriminating: on a steady tape the
    shift-less (lookahead) variant produces the identical pace and this test
    would prove nothing (review catch, verified by probe)."""
    n = 41
    rows = []
    for day in range(n, 0, -1):
        rows += _flat_day(day, vol=100.0 + (n - day))  # newer = busier
    sess = build_sessions(_frame(rows))
    # shift(1) costs one row: exactly PACE_MIN_SESSIONS leading NaNs
    assert pd.isna(sess["pace"].iloc[PACE_MIN_SESSIONS - 1])
    assert not pd.isna(sess["pace"].iloc[PACE_MIN_SESSIONS])
    f45 = sess["f45_vol"]
    expected = f45.iloc[-1] / f45.iloc[:-1].tail(PACE_SESSIONS).median()
    buggy = f45.iloc[-1] / f45.tail(PACE_SESSIONS).median()
    assert abs(expected - buggy) > 1e-9, "ramp must separate the two variants"
    assert abs(sess["pace"].iloc[-1] - expected) < 1e-12
    print("  PACE -> yardstick excludes today (exact-median check on a ramp)")


def test_pace_yardstick_skips_dead_proxy_sessions():
    """A 25-session proxy outage must VANISH from the yardstick, not sit in it
    as zeros — zeros as the window majority would zero the median and (with
    the med>0 guard) silently kill pace for every recovery session."""
    rows = []
    for day in range(46, 0, -1):
        rows += _flat_day(day, vol=(0.0 if day > 21 else 100.0))
    sess = build_sessions(_frame(rows))
    assert abs(sess["pace"].iloc[-1] - 1.0) < 1e-12
    # and the volume shares ignore outage sessions instead of diluting
    out = opening_study(_frame(rows))
    bar0 = out["volatility"]["per_bar"][0]
    assert abs(bar0["vol_share"] - 1 / 75) < 6e-5, "share diluted by dead days"
    print("  PACE/SHARES -> proxy outage excluded from yardstick and shares")


def test_study_refuses_thin_samples_and_labels_rates_vs_base():
    thin = _frame(_flat_day(1) + _flat_day(2))
    out = opening_study(thin)
    assert "nothing may be called" in out["note"]
    rows = []
    for day in range(40, 0, -1):
        rows += _flat_day(day)
    out = opening_study(_frame(rows))
    assert out["sessions"]["n"] == 40                 # all full, all qualify
    # flat synthetic days have f45_dir == 0 everywhere -> no continuation cells
    cont = out["continuation_10_to_close"]
    assert "note" in cont and "no cell may form" in cont["note"]
    td = out["trend_day"]
    assert "base" in td and 0.0 <= td["base"]["rate"] <= 1.0
    print("  STUDY -> thin sample refused; rate cells carry their base")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print("ALL OPENING TESTS PASSED")
