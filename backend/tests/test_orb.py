"""ORB prototype tests on hand-built sessions with known answers.

The dangerous bug in a breakout backtest is look-ahead: filling on the bar that
breaks (whose high/low you already know) instead of the next one. That single
mistake makes any ORB look profitable, so it is tested explicitly.

Run:  python backend/tests/test_orb.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from app.backtest.orb import OrbParams, run_orb

# 09:15 IST on an arbitrary day, as a UTC epoch.
DAY = 19676 * 86400
OPEN = DAY + (9 * 60 + 15) * 60 - 19800
MIN = 60


def bar(t, o, h, l, c, v=1000):
    return {"ts": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def session(bars):
    return pd.DataFrame(bars)


def _or_bars(hi=100.0, lo=95.0, start=OPEN, n=15):
    """n one-minute bars forming an opening range of exactly [lo, hi].

    Kept near ~5% wide so the default max_range_pct filter does not skip the
    session — the filters get their own test."""
    out = []
    for i in range(n):
        out.append(bar(start + i * MIN, 97, hi if i == 0 else 99, lo if i == 1 else 96, 97))
    return out


def test_long_breakout_hits_target():
    b = _or_bars()
    t = OPEN + 15 * MIN
    b.append(bar(t, 99, 101, 98, 101))              # close ABOVE 100 -> arms entry
    b.append(bar(t + MIN, 101, 102, 100, 102))      # entry fills at THIS open (101)
    for i in range(2, 40):                          # drift up to the target (110)
        b.append(bar(t + i * MIN, 101 + i, 102 + i, 100 + i, 101 + i))
    r = run_orb(session(b), OrbParams(or_minutes=15, rr=1.5), slippage_pts=0.0)
    assert r.trades_total == 1, r.trades_total
    tr = r.trades[0]
    assert tr.direction.value == "CE"
    assert tr.entry == 101.0, tr.entry                # next bar's OPEN, not the break bar
    assert tr.stop == 95.0, tr.stop                   # far side of the range
    assert tr.outcome == "target", tr.outcome
    assert abs(tr.r_multiple - 1.5) < 0.01, tr.r_multiple
    print(f"  LONG  -> entry {tr.entry} stop {tr.stop} target {tr.target} = {tr.r_multiple}R")


def test_no_lookahead_entry_is_next_bar_open():
    """The breakout bar spikes to 130 then the next opens at 101. A look-ahead
    implementation would enter near the spike; we must enter at 101."""
    b = _or_bars()
    t = OPEN + 15 * MIN
    b.append(bar(t, 99, 130, 98, 101))              # violent break bar
    b.append(bar(t + MIN, 101, 101.5, 100.5, 101))  # the real fill
    for i in range(2, 30):
        b.append(bar(t + i * MIN, 101, 102, 100, 101))
    r = run_orb(session(b), OrbParams(or_minutes=15, rr=1.5), slippage_pts=0.0)
    assert r.trades_total == 1
    assert r.trades[0].entry == 101.0, r.trades[0].entry
    print("  NO-LOOKAHEAD -> filled at next open 101, not the 130 spike")


def test_short_breakout_stops_out():
    b = _or_bars()
    t = OPEN + 15 * MIN
    b.append(bar(t, 96, 97, 93, 94))                # close BELOW 95 -> short
    b.append(bar(t + MIN, 94, 95, 93, 94))          # entry at 94, stop 100, risk 6
    b.append(bar(t + 2 * MIN, 94, 101, 93, 100))    # runs back through 100 -> stop
    for i in range(3, 20):
        b.append(bar(t + i * MIN, 100, 101, 99, 100))
    r = run_orb(session(b), OrbParams(or_minutes=15, rr=1.5), slippage_pts=0.0)
    assert r.trades_total == 1
    tr = r.trades[0]
    assert tr.direction.value == "PE" and tr.outcome == "stop", (tr.direction, tr.outcome)
    assert abs(tr.r_multiple + 1.0) < 0.01, tr.r_multiple   # a stop is exactly -1R
    print(f"  SHORT -> stopped at {tr.exit} = {tr.r_multiple}R")


def test_stop_wins_when_bar_spans_both():
    """Conservative: a bar containing stop AND target counts as the stop."""
    b = _or_bars()
    t = OPEN + 15 * MIN
    b.append(bar(t, 99, 101, 98, 101))
    b.append(bar(t + MIN, 101, 102, 100, 101))      # entry 101, stop 95, target 110
    b.append(bar(t + 2 * MIN, 101, 130, 80, 100))   # spans both
    for i in range(3, 12):
        b.append(bar(t + i * MIN, 100, 101, 99, 100))
    r = run_orb(session(b), OrbParams(or_minutes=15, rr=1.5), slippage_pts=0.0)
    assert r.trades[0].outcome == "stop", r.trades[0].outcome
    print("  SAME-BAR -> stop assumed before target")


def test_no_breakout_means_no_trade():
    b = _or_bars()
    t = OPEN + 15 * MIN
    for i in range(0, 30):                          # chops inside the range all day
        b.append(bar(t + i * MIN, 97, 99.5, 95.5, 97))
    r = run_orb(session(b), OrbParams(or_minutes=15), slippage_pts=0.0)
    assert r.trades_total == 0
    print("  INSIDE-DAY -> no trade")


def test_range_filters_skip_the_day():
    b = _or_bars()
    t = OPEN + 15 * MIN
    b.append(bar(t, 99, 101, 98, 101))
    for i in range(1, 20):
        b.append(bar(t + i * MIN, 101, 102, 100, 101))
    df = session(b)
    # range is 5 on ~97 => ~5.2%; a 1% cap must skip the day entirely
    assert run_orb(df, OrbParams(or_minutes=15, max_range_pct=1.0), slippage_pts=0.0).trades_total == 0
    # and a 20% floor must also skip it
    assert run_orb(df, OrbParams(or_minutes=15, min_range_pct=20.0), slippage_pts=0.0).trades_total == 0
    print("  FILTERS -> min/max range both skip the session")


def test_one_trade_per_day_by_default():
    b = _or_bars()
    t = OPEN + 15 * MIN
    b.append(bar(t, 99, 101, 98, 101))              # long break
    b.append(bar(t + MIN, 101, 102, 100, 101))
    b.append(bar(t + 2 * MIN, 101, 102, 85, 90))    # stops out, then breaks down
    for i in range(3, 25):
        b.append(bar(t + i * MIN, 90, 91, 89, 90))
    r = run_orb(session(b), OrbParams(or_minutes=15), slippage_pts=0.0)
    assert r.trades_total == 1, r.trades_total
    print("  ONE-PER-DAY -> reversal not taken unless allow_reversal")


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
