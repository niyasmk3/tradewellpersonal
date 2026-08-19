"""Expiry resolution and the CAS-aware close reference.

Both are places where a quiet wrong answer would silently move every number in
the Closing Day study: a mis-dated expiry rescales theta, and a mis-read close
flips the signal outright.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing.calendar import THURSDAY, TUESDAY, ExpiryCalendar
from app.closing.study import Day, close_ref


def _weekdays(start: date, n: int) -> list:
    """n consecutive Mon-Fri sessions from `start`."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_expiry_is_tuesday_after_the_switch_and_rolls_on_expiry_day():
    days = _weekdays(date(2026, 8, 3), 20)
    cal = ExpiryCalendar(days, date(2025, 9, 2))
    # Monday holds into Tuesday's expiry...
    assert cal.holdable_expiry(date(2026, 8, 17)) == date(2026, 8, 18)
    # ...but a trade opened ON expiry day cannot be held overnight in that
    # contract, so it rolls a week.
    assert cal.holdable_expiry(date(2026, 8, 18)) == date(2026, 8, 25)


def test_expiry_is_thursday_before_the_switch():
    days = _weekdays(date(2025, 8, 4), 15)
    cal = ExpiryCalendar(days, date(2025, 9, 2), THURSDAY, TUESDAY)
    assert cal.holdable_expiry(date(2025, 8, 4)) == date(2025, 8, 7)   # Mon -> Thu
    assert cal.holdable_expiry(date(2025, 8, 7)) == date(2025, 8, 14)  # Thu rolls


def test_expiry_rolls_back_over_a_holiday():
    days = [d for d in _weekdays(date(2026, 8, 3), 20) if d != date(2026, 8, 18)]
    cal = ExpiryCalendar(days, date(2025, 9, 2))
    # Tuesday 18-Aug is not a session, so settlement moves to Monday 17-Aug —
    # and a Monday trade can therefore no longer hold that contract overnight.
    assert cal.holdable_expiry(date(2026, 8, 14)) == date(2026, 8, 17)
    assert cal.holdable_expiry(date(2026, 8, 17)) == date(2026, 8, 25)


def test_expiry_past_the_data_edge_is_not_rolled_back():
    """Regression: the holiday roll-back used to walk backwards past the last
    stored session and land on it, dating the newest trade's expiry days early
    (DTE 7 became DTE 1, roughly doubling its modelled return)."""
    days = _weekdays(date(2026, 8, 10), 8)   # ends Wed 19-Aug
    cal = ExpiryCalendar(days, date(2025, 9, 2))
    assert cal.holdable_expiry(date(2026, 8, 18)) == date(2026, 8, 25)
    assert cal.holdable_expiry(date(2026, 8, 19)) == date(2026, 8, 25)


def _day(bars: dict) -> Day:
    d = Day(date(2026, 8, 17))
    for hm, (o, h, l, c) in bars.items():
        d.bars[hm] = (o, h, l, c, 0)
    return d


def test_close_ref_takes_the_last_bar_when_the_tape_never_froze():
    day = _day({(15, 0): (100, 101, 99, 100.5),
                (15, 10): (100.5, 102, 100, 101.5),
                (15, 25): (101.5, 103, 101, 102.5)})
    assert close_ref(day) == 102.5


def test_close_ref_steps_over_the_cas_freeze_and_its_auction_print():
    """17-Aug-2026 shape: tape freezes at 15:15, then a separate auction print
    lands at 15:25 that is NOT the close. Taking the last bar would read 24287
    where the close was 24339 — a 50-point error, easily enough to flip a
    signal."""
    day = _day({(15, 5): (24351.0, 24353.7, 24341.35, 24349.4),
                (15, 10): (24348.75, 24360.1, 24330.7, 24339.6),
                (15, 15): (24339.8, 24339.8, 24339.8, 24339.8),
                (15, 20): (24339.8, 24339.8, 24339.8, 24339.8),
                (15, 25): (24339.8, 24339.8, 24287.65, 24287.65)})
    assert close_ref(day) == 24339.6


def test_close_ref_ignores_a_flat_bar_before_the_freeze_window():
    """A genuinely still 5-min bar earlier in the day is not an auction."""
    day = _day({(11, 0): (100.0, 100.0, 100.0, 100.0),
                (15, 0): (100, 101, 99, 100.5),
                (15, 25): (100.5, 102, 100, 101.5)})
    assert close_ref(day) == 101.5
