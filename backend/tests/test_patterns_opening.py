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
    assemble_opening_live,
    build_sessions,
    fade_backtest,
    opening_study,
    recent_mornings,
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


_STUDY_STUB = {
    "opening": {
        "sessions": {"n": 99},
        "or45_tercile_bps": [5.0, 15.0],
        "gap": {"by_bucket": {"big_up": {"marker": "gap-cell"}}},
        "continuation_10_to_close": {"by_gap": {"big_up": {"marker": "cont-cell"}}},
        "trend_day": {"by_gap": {"big_up": {"marker": "trend-cell"}}},
        "or30_breakout": {"marker": "orb-cell"},
    },
    "volume_pace": {"cum_median": [100.0 * (i + 1) for i in range(75)]},
}


def test_live_frozen_features_match_the_study_builder():
    """The whole point of the live read: at 10:00 its state must equal what
    build_sessions would later compute for the same day — one definition of
    the features, not two drifting ones."""
    prev = _flat_day(2, px=100.0)
    day = _flat_day(1, px=101.0)                      # big_up gap day
    day[4].update(high=101.6, low=100.8)
    # Nonzero first-45 direction: a flat day pins f45_dir at 0 == 0, which a
    # sign flip in the live read would pass (review catch).
    day[8].update(close=101.2, high=101.25)
    live = assemble_opening_live(_frame(day), _frame(prev), _STUDY_STUB)
    combined = build_sessions(_frame(prev + day)).iloc[-1]
    st = live["state"]
    assert live["window_complete"] and st["aligned_0915"]
    assert st["gap_bps"] == combined["gap_bps"]
    assert st["gap_bucket"] == combined["gap_bucket"] == "big_up"
    assert st["or45_bps_so_far"] == combined["or45_bps"]
    assert st["f45_dir_so_far"] == combined["f45_dir"] == 1
    assert st["above_proxy_vwap"] == combined["above_vwap"]
    # OR15/OR30 LEVELS reconciled against the builder's bps (review catch:
    # presence was asserted, values never were)
    o = 101.0
    assert round((st["or15"]["high"] - st["or15"]["low"]) / o * 1e4, 1) == combined["or15_bps"]
    assert round((st["or30"]["high"] - st["or30"]["low"]) / o * 1e4, 1) == combined["or30_bps"]
    assert st["or45_tercile"] == "large"              # 79bps vs [5, 15]
    assert st["prev_session"]["close"] == 100.0
    # cum vol 9*100 vs curve[8]=900 -> exactly 1.0x
    assert st["pace_vs_typical"] == 1.0
    # matched cells pass through from the stored study verbatim
    assert live["matched"]["gap"] == {"marker": "gap-cell"}
    assert live["matched"]["continuation"] == {"marker": "cont-cell"}
    assert live["matched"]["trend_day"] == {"marker": "trend-cell"}
    assert live["matched"]["or30_breakout"] == {"marker": "orb-cell"}
    print("  LIVE -> frozen 10:00 state == build_sessions, cells pass through")


def test_live_mid_window_empty_and_misaligned():
    prev = _flat_day(2, px=100.0)
    # empty today
    live = assemble_opening_live(pd.DataFrame(), _frame(prev), _STUDY_STUB)
    assert live["bars_in_window"] == 0 and live["state"] is None
    # mid-window: 4 closed bars — OR15 set, OR30/tercile absent, gap matched
    live = assemble_opening_live(_frame(_flat_day(1, px=101.0)[:4]),
                                 _frame(prev), _STUDY_STUB)
    st = live["state"]
    assert not live["window_complete"] and live["bars_in_window"] == 4
    assert st["or15"] is not None and st["or30"] is None
    assert "or45_tercile" not in st
    assert st["gap_bucket"] == "big_up" and live["matched"]["gap"] == {"marker": "gap-cell"}
    # misaligned: first closed bar is 09:30 — no gap, no gap-keyed cells, and
    # CRUCIALLY no pace/vwap (bar count is not time-of-day when the anchor
    # slips — the review's probe showed a wrong 1.0x served here)
    live = assemble_opening_live(_frame(_flat_day(1, px=101.0)[3:]),
                                 _frame(prev), _STUDY_STUB)
    st = live["state"]
    assert not st["aligned_0915"] and st["gap_bps"] is None
    assert "pace_vs_typical" not in st and "above_proxy_vwap" not in st
    assert st["or15"] is None, "a 09:30 anchor must not relabel bars as OR15"
    assert live["matched"] == {"or30_breakout": {"marker": "orb-cell"}}
    print("  LIVE -> empty/mid-window/misaligned degrade honestly")


def test_recent_mornings_grades_and_headline_tallies():
    rows = _flat_day(5, px=100.0) + _flat_day(4, px=100.0)
    # big_up FADE day: +100bps gap, morning up, afternoon below 10:00 close,
    # lows never reach 100 -> unfilled
    fade = _flat_day(3, px=101.0)
    fade[8].update(close=101.5, high=101.55)          # f45_dir = +1
    for k in range(9, 75):
        fade[k].update(open=100.8, close=100.8, high=100.85, low=100.75)
    # big_down TREND day vs fade day's 100.8 close: 99 open, walks down
    trend = _flat_day(2, px=99.0)
    for k in range(9, 75):
        px = 99.0 - 0.02 * (k - 8)
        trend[k].update(open=px + 0.02, close=px, high=px + 0.05, low=px - 0.05)
    # flat-gap day vs trend day's ~97.68 close
    flat = _flat_day(1, px=97.68)
    rows += fade + trend + flat
    board = recent_mornings(_frame(rows), {}, n=10)
    by_bucket = {m["gap_bucket"]: m for m in board["mornings"]}
    f = by_bucket["big_up"]
    assert f["outcomes"]["continued_to_close"] is False
    assert f["outcomes"]["filled_by_close"] is False
    assert f["outcomes"]["gap_faded"] is True
    t = by_bucket["big_down"]
    assert t["outcomes"]["trend_day"] is True
    assert t["outcomes"]["filled_by_close"] is False
    heads = {h["tendency"]: h for h in board["headline"]}
    assert heads["big gaps don't fill by close"] == {
        "tendency": "big gaps don't fill by close", "n": 2, "hits": 2}
    assert heads["big gap-up morning fades to close"]["hits"] == 1
    assert heads["big gap-down day trends"]["hits"] == 1
    print("  SCOREBOARD -> outcomes graded per morning; headline tallies exact")


def _big_up_fade_day(day: int, prev_px: float = 100.0, afternoon: float = 100.6):
    """Big gap up (+100bps), morning rises (f45_dir=+1: bar 8 closes 101.4),
    afternoon settles at `afternoon` — fade SHORT enters 101.4 at 10:00."""
    d = _flat_day(day, px=101.0)
    d[8].update(close=101.4, high=101.45)
    for k in range(9, 75):
        d[k].update(open=afternoon, close=afternoon,
                    high=afternoon + 0.05, low=afternoon - 0.05)
    return d


def test_fade_backtest_exact_pnls_and_stop():
    rows = _flat_day(4, px=100.0)
    rows += _big_up_fade_day(3)                        # winner: 101.4 -> 100.6
    bt = fade_backtest(_frame(rows))
    assert "note" in bt and "no verdict" in bt["note"].lower()

    # 40 winners: enough for a verdict, exact numbers. Interleaved flat days
    # (each closing 100.0) reset the prev close so every fade day gaps +100bps.
    rows = _flat_day(90, px=100.0)
    for i in range(40):
        rows += _big_up_fade_day(89 - 2 * i)
        rows += _flat_day(88 - 2 * i, px=100.0)
    bt = fade_backtest(_frame(rows))
    raw = bt["raw_hold_to_close"]
    assert raw["n"] == 40
    expected = (101.4 - 100.6) / 101.4 * 1e4           # short fade GAIN, exact
    assert abs(raw["avg_bps"] - round(expected, 1)) < 0.2
    assert raw["win_rate"] == 1.0
    # stopped variant: no stop day here -> identical to raw
    st = bt["stopped_at_or45_extreme"]
    assert st["stop_hit_rate"] == 0.0
    assert abs(st["avg_bps"] - raw["avg_bps"]) < 0.2
    # MAE on these clean winners: afternoon high 100.65 < entry 101.4 -> 0
    assert bt["mae_of_raw_hold_bps"]["median"] == 0.0
    print("  FADE BT -> exact short-fade pnl, stop untouched, MAE zero")


def test_fade_backtest_stop_hit_is_conservative_gap_through():
    rows = _flat_day(121, px=100.0)
    days = []
    for i in range(35):
        days.append(_big_up_fade_day(120 - 2 * i))
        days.append(_flat_day(119 - 2 * i, px=100.0))
    # replace the first fade day with the stop day: afternoon OPENS at 102.3,
    # beyond the OR45 high 102.05 -> gap-through exit at the OPEN. The close
    # (102.6) differs from the open so exit-at-close grades -118.3, not -88.8
    # — the review proved the two were indistinguishable in the old frame.
    stop_day = _flat_day(120, px=101.0)
    stop_day[8].update(close=101.4, high=102.05)       # or45 high = 102.05
    for k in range(9, 75):
        stop_day[k].update(open=102.3, close=102.6, high=102.65, low=102.25)
    days[0] = stop_day
    for d in days:
        rows += d
    bt = fade_backtest(_frame(rows))
    st = bt["stopped_at_or45_extreme"]
    assert st["n"] == 35
    assert abs(st["stop_hit_rate"] - round(1 / 35, 3)) < 1e-9
    # graded at the 102.3 OPEN: not the stop (102.05 -> -64.1) and not the
    # close (102.6 -> -118.3) — both mutations now fail this exact value
    worst_expected = (101.4 - 102.3) / 101.4 * 1e4
    assert abs(st["worst_bps"] - round(worst_expected, 1)) < 0.05
    # the raw variant held to the 102.6 close and must be STRICTLY worse
    raw_worst = (101.4 - 102.6) / 101.4 * 1e4
    assert abs(bt["raw_hold_to_close"]["worst_bps"] - round(raw_worst, 1)) < 0.05
    assert bt["raw_hold_to_close"]["worst_bps"] < st["worst_bps"]
    print("  FADE BT -> gap-through stop graded at the open, conservatively")


def test_fade_backtest_wick_to_stop_grades_at_the_stop_level():
    """The review proved three stop mutations survived the old tests: a
    wrong-side stop, a wick graded at the bar's high, and gap-through vs wick
    confusion. This day kills all three: bar 9 OPENS inside (101.5), WICKS
    through the short stop (102.05, high 102.10), and the afternoon settles
    at a would-be-winning 101.0 — only exit-AT-the-stop yields -64.1bps."""
    rows = _flat_day(121, px=100.0)
    days = []
    for i in range(35):
        days.append(_big_up_fade_day(120 - 2 * i))
        days.append(_flat_day(119 - 2 * i, px=100.0))
    wick = _flat_day(120, px=101.0)
    wick[8].update(close=101.4, high=102.05)           # short stop = 102.05
    wick[9].update(open=101.5, high=102.10, low=101.35, close=101.4)
    for k in range(10, 75):
        wick[k].update(open=101.0, close=101.0, high=101.05, low=100.95)
    days[0] = wick
    for d in days:
        rows += d
    st = fade_backtest(_frame(rows))["stopped_at_or45_extreme"]
    assert st["n"] == 35
    assert abs(st["stop_hit_rate"] - round(1 / 35, 3)) < 1e-9
    expected = (101.4 - 102.05) / 101.4 * 1e4          # AT the stop: -64.1
    # wrong-side stop would gap-through at 101.5 (-9.9); at-the-high grades
    # 102.10 (-69.0); both fail this exact value
    assert abs(st["worst_bps"] - round(expected, 1)) < 0.05
    print("  FADE BT -> wick-to-stop graded AT the stop; mutations killed")


def test_fade_backtest_long_side_wick_and_mirror():
    rows = _flat_day(90, px=100.0)
    days = []
    for i in range(31):
        # big gap UP but morning goes DOWN: bar 8 closes 100.7 (f45_dir=-1),
        # afternoon recovers to 101.2 -> fade is a LONG that wins
        d = _flat_day(89 - 2 * i, px=101.0)
        d[8].update(close=100.7, low=100.65)
        for k in range(9, 75):
            d[k].update(open=101.2, close=101.2, high=101.25, low=101.15)
        days.append(d)
        days.append(_flat_day(88 - 2 * i, px=100.0))
    # replace one with a LONG-side wick: bar 9 opens ABOVE the or45-low stop
    # (100.65), wicks down through it (low 100.60) -> graded AT the stop:
    # (100.65-100.7)/100.7 = -5.0bps. A wrong-side stop (101.05) would
    # gap-through at the 100.9 open (+19.9); grading at the bar low gives -9.9.
    wick = _flat_day(89, px=101.0)
    wick[8].update(close=100.7, low=100.65)
    wick[9].update(open=100.9, high=100.95, low=100.60, close=100.9)
    for k in range(10, 75):
        wick[k].update(open=101.2, close=101.2, high=101.25, low=101.15)
    days[0] = wick
    for d in days:
        rows += d
    bt = fade_backtest(_frame(rows))
    raw = bt["raw_hold_to_close"]
    assert raw["n"] == 31 and raw["win_rate"] == 1.0   # raw holds ride to 101.2
    expected = (101.2 - 100.7) / 100.7 * 1e4           # long fade, exact
    assert abs(raw["avg_bps"] - round(expected, 1)) < 0.2
    st = bt["stopped_at_or45_extreme"]
    assert abs(st["stop_hit_rate"] - round(1 / 31, 3)) < 1e-9
    assert abs(st["worst_bps"] - round((100.65 - 100.7) / 100.7 * 1e4, 1)) < 0.05
    assert bt["big_down_mirror_raw"]["n"] == 0         # no big_down days here
    assert "by_year_raw" in bt
    print("  FADE BT -> long-side fade + wick exact; mirror empty on this frame")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print("ALL OPENING TESTS PASSED")
