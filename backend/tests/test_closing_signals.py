"""The candidate sweep's own honesty machinery.

The sweep exists to answer "which 15:00 rule picks the side", and the two ways
that question gets answered wrongly are both tested here: crediting a rule for
NIFTY's upward drift, and reading a rule that has no signal at all as if it
did. The controls are the test.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing.calendar import IST
from app.closing.signals import CANDIDATES, build_rows, evaluate, search
from app.closing.study import build_days

import pandas as pd

# The sweep reads the 09:15 open, the 09:50 exit print, 13:00/14:00 for the
# momentum candidates, 15:00 for signal+fill, and 15:25 so close_ref has a
# session end to walk back from.
BARS = [(9, 15), (9, 50), (13, 0), (14, 0), (15, 0), (15, 25)]
_MORNING = {(9, 15), (9, 50)}


def _spine(levels: list) -> pd.DataFrame:
    """levels: [(date, open_level, close_level)] -> one synthetic session each.

    The morning bars sit at the open level and the afternoon bars at the close
    level, so a night's realised move is exactly next-session-open minus this
    session's 15:00 close — which is what the sweep's target measures.
    """
    rows = []
    for d, o, c in levels:
        for hh, mm in BARS:
            ts = int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp())
            v = o if (hh, mm) in _MORNING else c
            rows.append((ts, v, v, v, v))
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])


def _weekdays(start: date, n: int) -> list:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def _rows(levels):
    return build_rows(build_days(_spine(levels)))


def test_always_ce_control_measures_exactly_the_drift():
    """The control is only a control if it reports the unconditional drift. A
    rule that cannot beat it has found nothing, so this number has to be right."""
    days = _weekdays(date(2025, 1, 6), 60)
    # Each session rises 10 points intraday and gaps a further 5 overnight, so
    # the unconditional drift is a known, non-zero +5.
    levels, lvl = [], 20000.0
    for d in days:
        close = lvl + 10
        levels.append((d, lvl, close))
        lvl = close + 5
    out = evaluate(_rows(levels))
    ce = {r["key"]: r for r in out["results"]}["always_ce"]
    pe = {r["key"]: r for r in out["results"]}["always_pe"]
    assert ce["mean_pts"] == out["drift_pts"]
    assert pe["mean_pts"] == -out["drift_pts"]
    # A constant exposure earns the drift, never skill.
    assert ce["skill_pts"] == 0.0
    assert pe["skill_pts"] == 0.0
    assert ce["ce_share_pct"] == 100.0


def test_skill_strips_drift_from_a_rule_that_is_merely_long():
    """A rule that always says CE but is dressed up as a signal must score zero
    skill, however good its raw mean looks."""
    days = _weekdays(date(2025, 1, 6), 60)
    levels, lvl = [], 20000.0
    for d in days:
        close = lvl + 10          # every session closes above its own open
        levels.append((d, lvl, close))
        lvl = close + 5           # and every night gaps up
    out = evaluate(_rows(levels))
    day_open = {r["key"]: r for r in out["results"]}["day_open"]
    assert day_open["ce_share_pct"] == 100.0     # green every day -> always CE
    assert day_open["mean_pts"] == 5.0           # and it looks profitable
    assert day_open["skill_pts"] == 0.0          # but it is only the drift


def test_a_genuinely_predictive_rule_scores_positive_skill():
    """Construct a tape where the day's body really does predict the night:
    green sessions gap up next morning, red sessions gap down."""
    days = _weekdays(date(2025, 1, 6), 80)
    levels, lvl = [], 20000.0
    for i, d in enumerate(days):
        up = i % 2 == 0
        close = lvl + (40 if up else -40)
        levels.append((d, lvl, close))
        lvl = close + (60 if up else -60)   # overnight follows the body
    out = evaluate(_rows(levels))
    res = {r["key"]: r for r in out["results"]}
    assert res["day_open"]["skill_pts"] > 0
    assert res["day_open"]["hit_pct"] > 90
    # ...and its mirror image must lose exactly as much.
    assert res["fade_open"]["skill_pts"] < 0


def test_small_cells_are_dropped_rather_than_reported():
    days = _weekdays(date(2025, 1, 6), 10)
    levels = [(d, 20000.0, 20010.0) for d in days]
    out = evaluate(_rows(levels))
    assert out["results"] == []      # 10 sessions is below MIN_SAMPLE


def test_search_publishes_its_candidate_count_and_survivor_rule():
    days = _weekdays(date(2025, 1, 6), 120)
    levels, lvl = [], 20000.0
    for i, d in enumerate(days):
        up = i % 3 != 0
        close = lvl + (30 if up else -30)
        levels.append((d, lvl, close))
        lvl = close + (20 if up else -20)
    out = search(build_days(_spine(levels)), today=days[-1])
    assert out["available"]
    assert out["candidates_tested"] == len(CANDIDATES)
    # Survivors are never controls — "always CE" must not be sold as a finding.
    assert all(not k.startswith("always") for k in out["survivors"])
    assert "UNCORRECTED" in out["note"]


def test_lasthr_confirm_stands_aside_on_a_disagreement_night():
    """The 14-Aug-2026 shape: day green at 15:00 but falling through the last
    hour. The confirmation rule must return no trade — not CE (the body) and
    not PE (the fade, which scored negative skill in every window)."""
    from app.closing.signals import CANDIDATES
    fn = {k: f for k, _l, f in CANDIDATES}["lasthr_confirm"]
    disagree = {"p1500": 24374.0, "open": 24361.9, "p1400": 24395.0}
    assert fn(disagree) is None
    agree_up = {"p1500": 24400.0, "open": 24350.0, "p1400": 24380.0}
    assert fn(agree_up) == 1
    agree_dn = {"p1500": 24300.0, "open": 24350.0, "p1400": 24320.0}
    assert fn(agree_dn) == -1
