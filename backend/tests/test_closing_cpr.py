"""The CPR shadow signal: frozen math, the unknown-never-passes rule, and
that it rides the card without touching the verdict."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import cpr, tonight
from app.closing.calendar import IST
from app.closing.study import Day


def test_levels_match_ochoa_and_friday_21_aug():
    # Friday 21-Aug-2026: H 24284.0 L 24206.8 C 24234.8 -> Monday's CPR.
    lv = cpr.levels(24284.0, 24206.8, 24234.8)
    assert lv["p"] == round((24284.0 + 24206.8 + 24234.8) / 3, 2)
    assert lv["bc"] == 24238.3 and lv["tc"] == 24245.4 or (lv["bc"] < lv["p"] < lv["tc"])
    assert lv["bc"] <= lv["p"] <= lv["tc"]            # always ordered, whichever side TC lands
    assert lv["width_pts"] == round(lv["tc"] - lv["bc"], 2)
    assert lv["narrow"] is True and lv["width_pct"] < cpr.NARROW_PCT


def test_width_is_distance_of_close_from_range_midpoint():
    """The non-obvious property: TC-BC = (2/3)*|C - (H+L)/2|. A huge-range
    day that closes dead centre has a ZERO-width CPR; a small-range day that
    closes on its high can be wide. 'Narrow' means 'closed mid-range', not
    'quiet day'."""
    assert cpr.levels(24900.0, 23800.0, 24350.0)["width_pts"] == 0.0
    lv = cpr.levels(24900.0, 23800.0, 24880.0)
    assert abs(lv["width_pts"] - (2 / 3) * abs(24880.0 - 24350.0)) < 0.01
    assert lv["narrow"] is False
    small = cpr.levels(24330.0, 24270.0, 24330.0)       # 60-pt day closing on its high
    assert abs(small["width_pts"] - 20.0) < 0.01 and small["narrow"] is True  # 0.082% — just inside


def test_wide_session_is_not_narrow_and_threshold_is_frozen():
    assert cpr.NARROW_PCT == 0.083
    assert cpr.REGISTERED_ON == "2026-08-22"
    lv = cpr.levels(24500.0, 24100.0, 24450.0)        # a 400-pt day closing near the high
    assert lv["narrow"] is False and lv["width_pct"] > cpr.NARROW_PCT


def _day(d: date, bars: dict) -> Day:
    day = Day(d)
    ts0 = int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())
    for (h, m), (o, hi, lo, c) in bars.items():
        day.bars[(h, m)] = (o, hi, lo, c, ts0 + h * 3600 + m * 60)
    return day


def test_from_session_uses_full_range_and_cas_aware_close():
    d = date(2026, 8, 20)
    day = _day(d, {(9, 15): (100.0, 101.0, 99.0, 100.5),
                   (15, 0): (100.5, 104.0, 98.0, 103.0),
                   (15, 10): (103.0, 103.5, 102.5, 103.2),
                   (15, 20): (103.2, 103.2, 103.2, 103.2),   # CAS freeze bar
                   (15, 35): (110.0, 110.0, 110.0, 110.0)})  # auction print, stepped over
    lv = cpr.from_session(day)
    assert lv == cpr.levels(110.0, 98.0, 103.2)        # H/L span the whole tape, C = last free bar
    assert cpr.from_session(None) is None
    assert cpr.from_session(Day(d)) is None


def test_annotate_stamps_previous_session_and_unknown_stays_none():
    d0, d1 = date(2026, 8, 19), date(2026, 8, 20)
    days = {d0: _day(d0, {(15, 0): (100.0, 100.05, 99.95, 100.0)}),   # razor-thin range
            d1: _day(d1, {(15, 0): (100.0, 101.0, 99.0, 100.0)})}
    trades = [{"date": d1.isoformat()}, {"date": d0.isoformat()}]
    cpr.annotate(trades, days)
    assert trades[0]["f_cpr_narrow"] is True and trades[0]["cpr_width_pct"] is not None
    assert trades[1]["f_cpr_narrow"] is None and trades[1]["cpr_width_pct"] is None  # first session: no prior


def test_summary_splits_clean_by_narrow_with_flagged_control():
    split = date(2026, 1, 1)
    tr = [
        {"date": "2025-06-01", "card_verdict": "CLEAN", "f_cpr_narrow": True, "net_pct": 40.0, "net_rs": 400},
        {"date": "2025-06-02", "card_verdict": "CLEAN", "f_cpr_narrow": False, "net_pct": -10.0, "net_rs": -100},
        {"date": "2025-06-03", "card_verdict": "FLAGGED", "f_cpr_narrow": True, "net_pct": -20.0, "net_rs": -200},
        {"date": "2026-03-01", "card_verdict": "CLEAN", "f_cpr_narrow": None, "net_pct": 5.0, "net_rs": 50},
    ]
    s = cpr.summary(tr, split)
    w = s["windows"]
    assert w["in_sample_2y"]["clean_narrow"]["n"] == 1 and w["in_sample_2y"]["clean_narrow"]["mean_pct"] == 40.0
    assert w["in_sample_2y"]["clean_rest"]["n"] == 1
    assert w["in_sample_2y"]["flagged_narrow"]["n"] == 1
    assert w["holdout_1y"]["clean_narrow"]["n"] == 0 and w["holdout_1y"]["clean_rest"]["n"] == 0  # unknown counts nowhere
    assert s["narrow_pct"] == cpr.NARROW_PCT and "never a gate" in s["definition"]


def test_card_carries_cpr_as_shadow_without_touching_verdict():
    from tests.test_closing_tonight import EXPIRY, NEXT, NOW, PREV, TODAY, _tape, _vix
    tape = _tape()
    r = tonight.evaluate(tape, _vix(), TODAY, NOW, NEXT, EXPIRY)
    v = r["values"]
    expect = cpr.from_session(tape[PREV])
    assert v["cpr_width_pct"] == expect["width_pct"] and v["cpr_narrow"] == expect["narrow"]
    assert v["cpr_bc"] == expect["bc"] and v["cpr_tc"] == expect["tc"]
    assert [c["key"] for c in r["checks"]] == ["lasthr", "volexp", "midrange", "bridge", "monthend"]
    assert r["verdict"] == "CLEAN"
    # Same tape, a previous session that closed far from its midpoint (wide
    # CPR): verdict unchanged, chip flips.
    wide = _tape()
    wide[PREV].bars[(15, 10)] = (24350.0, 24900.0, 23800.0, 24880.0)
    r2 = tonight.evaluate(wide, _vix(), TODAY, NOW, NEXT, EXPIRY)
    assert r2["values"]["cpr_narrow"] is False and r2["verdict"] == "CLEAN"
