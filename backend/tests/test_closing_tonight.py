"""The live 15:00 pre-trade card: check semantics, verdict rules, and the
first-write-wins decision-time log.

The card reads the five REGISTERED checks — the tests pin that a CLEAN verdict
needs all five strictly clear (unknowns never pass), that the 14-Aug shape
flags, and that a plain weekend is not a bridge (the frozen semantics)."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time as dtime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import tonight
from app.closing.calendar import IST
from app.closing.study import Day
from app.market import calendar as mcal

TODAY = date(2026, 8, 20)          # Thursday
NEXT = date(2026, 8, 21)           # Friday
PREV = date(2026, 8, 19)
EXPIRY = date(2026, 8, 25)         # Tuesday
# 15:06 — past the 15:05 settle clock, so the read is full, not provisional.
NOW = datetime(2026, 8, 20, 15, 6, tzinfo=IST)


def _day(d: date, bars: dict) -> Day:
    day = Day(d)
    ts0 = int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())
    for (h, m), (o, hi, lo, c) in bars.items():
        day.bars[(h, m)] = (o, hi, lo, c, ts0 + h * 3600 + m * 60)
    return day


def _tape(p0915=24300.0, p1400=24380.0, p1500=24400.0, hi=24420.0, lo=24290.0):
    """A green day whose 15:00 print sits near the top of the range."""
    return {
        TODAY: _day(TODAY, {(9, 15): (p0915, p0915 + 10, lo, p0915 + 5),
                            (14, 0): (p1400, hi, p1400 - 10, p1400 + 5),
                            (15, 0): (p1500, p1500 + 5, p1500 - 5, p1500 + 2)}),
        PREV: _day(PREV, {(15, 10): (24350.0, 24355.0, 24345.0, 24352.0)}),
    }


def _vix(v0915=11.2, v1500=11.6, prev_close=11.3):
    out = {PREV: {(15, 25): prev_close}}
    today = {}
    if v0915 is not None:
        today[(9, 15)] = v0915
    if v1500 is not None:
        today[(15, 0)] = v1500
    out[TODAY] = today
    return out


def _by_key(result):
    return {c["key"]: c for c in result["checks"]}


def test_clean_night_is_clean_and_carries_trade_values():
    r = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY)
    assert r["available"] and not r["provisional"]
    assert r["verdict"] == "CLEAN"
    assert [c["status"] for c in r["checks"]] == ["clear"] * 5
    v = r["values"]
    assert v["direction"] == "CE"
    assert v["gap_pts"] == 100.0
    assert v["strike"] == 24400.0
    assert v["expiry"] == EXPIRY.isoformat()
    assert v["carry_days"] == 1
    assert v["prev_close"] == 24352.0


def test_14aug_shape_flags_lasthr():
    """Day green but the last hour falling — the documented stand-aside."""
    r = tonight.evaluate(_tape(p1400=24420.0, p1500=24400.0), _vix(),
                         TODAY, NOW, NEXT, EXPIRY)
    assert r["verdict"] == "FLAGGED"
    assert "lasthr" in r["red"]


def test_no_vol_expansion_flags():
    """Both legs known and non-positive -> red."""
    r = tonight.evaluate(_tape(), _vix(v0915=11.9, v1500=11.1, prev_close=11.3),
                         TODAY, NOW, NEXT, EXPIRY)
    assert "volexp" in r["red"]


def test_single_unknown_vix_leg_is_unknown_and_blocks_clean():
    """Prev-session VIX missing and the one known leg negative: the missing
    leg may genuinely have fired, so the check is UNKNOWN — never red, never
    clear — and an unknown means the night cannot be CLEAN."""
    vix = {TODAY: {(9, 15): 11.9, (15, 0): 11.1}}   # no PREV session at all
    r = tonight.evaluate(_tape(), vix, TODAY, NOW, NEXT, EXPIRY)
    assert _by_key(r)["volexp"]["status"] == "unknown"
    assert r["verdict"] == "INCOMPLETE"


def test_midrange_print_flags():
    r = tonight.evaluate(_tape(p1500=24355.0, hi=24420.0, lo=24290.0), _vix(),
                         TODAY, NOW, NEXT, EXPIRY)
    assert "midrange" in r["red"]


def test_bridge_semantics_weekend_is_not_a_bridge():
    friday = tonight.evaluate(_tape(), _vix(), TODAY, NOW,
                              TODAY + timedelta(days=3), EXPIRY)
    assert _by_key(friday)["bridge"]["status"] == "clear"
    two_day = tonight.evaluate(_tape(), _vix(), TODAY, NOW,
                               TODAY + timedelta(days=2), EXPIRY)
    assert "bridge" in two_day["red"]
    long_gap = tonight.evaluate(_tape(), _vix(), TODAY, NOW,
                                TODAY + timedelta(days=4), EXPIRY)
    assert "bridge" in long_gap["red"]


def test_month_end_flags():
    r = tonight.evaluate(_tape(), _vix(), TODAY, NOW, date(2026, 9, 1), EXPIRY)
    assert "monthend" in r["red"]


def test_provisional_before_1500_uses_latest_close():
    tape = _tape()
    del tape[TODAY].bars[(15, 0)]
    r = tonight.evaluate(tape, _vix(), TODAY,
                         datetime(2026, 8, 20, 14, 30, tzinfo=IST), NEXT, EXPIRY)
    assert r["provisional"] is True
    assert r["values"]["p1500"] == 24385.0   # the 14:00 bar's close stands in
    assert "final read at 15:00" in r["signal_time"]


def test_forming_1500_bar_stays_provisional_until_settle():
    """Kite serves the in-progress (15,0) candle from 15:00:01, but its
    close/high/low keep moving until 15:05 — a read in that window must be
    provisional (and therefore unloggable), even though the print itself is
    final. Review catch: a 15:01 read could otherwise freeze a CLEAN that the
    settled bar scores FLAGGED."""
    at_1501 = tonight.evaluate(_tape(), _vix(), TODAY,
                               datetime(2026, 8, 20, 15, 1, tzinfo=IST), NEXT, EXPIRY)
    assert at_1501["provisional"] is True
    assert at_1501["values"]["p1500"] == 24400.0        # the print IS final
    assert "settle at 15:05" in at_1501["signal_time"]
    at_1506 = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY)
    assert at_1506["provisional"] is False
    assert tonight.SETTLE_HM == dtime(15, 5)


def test_flat_body_is_a_red_lasthr_and_no_direction():
    """A dead-flat day: no side to pick, and the flat-leg guard must read
    red (stand aside), never accidentally 'agree'."""
    r = tonight.evaluate(_tape(p0915=24400.0, p1500=24400.0), _vix(),
                         TODAY, NOW, NEXT, EXPIRY)
    assert r["values"]["direction"] is None
    assert "lasthr" in r["red"]
    assert r["verdict"] == "FLAGGED"


def test_volexp_one_known_positive_leg_is_clear():
    """Prev-session VIX missing but the known 09:15 leg positive: a known
    positive leg fires the flag regardless of the missing one."""
    vix = {TODAY: {(9, 15): 11.2, (15, 0): 11.6}}   # no PREV session
    r = tonight.evaluate(_tape(), vix, TODAY, NOW, NEXT, EXPIRY)
    assert _by_key(r)["volexp"]["status"] == "clear"


def test_unresolved_next_day_makes_calendar_checks_unknown():
    r = tonight.evaluate(_tape(), _vix(), TODAY, NOW, None, EXPIRY)
    assert _by_key(r)["bridge"]["status"] == "unknown"
    assert _by_key(r)["monthend"]["status"] == "unknown"
    assert r["verdict"] == "INCOMPLETE"


def test_uncovered_calendar_year_makes_calendar_checks_unknown():
    """mcal fails open on years without a holiday list; the card must not
    turn that guess into a definitive 'clear' — unknown, never silent."""
    r = tonight.evaluate(_tape(), _vix(), TODAY, NOW, date(2027, 1, 1), EXPIRY)
    assert _by_key(r)["bridge"]["status"] == "unknown"
    assert "2027" in _by_key(r)["bridge"]["detail"]
    assert _by_key(r)["monthend"]["status"] == "unknown"
    assert r["verdict"] == "INCOMPLETE"


def test_next_trading_day_skips_weekend_and_holiday(monkeypatch):
    assert tonight.next_trading_day(date(2026, 8, 21)) == date(2026, 8, 24)  # Fri -> Mon
    monkeypatch.setattr(mcal, "HOLIDAYS", set(mcal.HOLIDAYS) | {"2026-08-24"})
    assert tonight.next_trading_day(date(2026, 8, 21)) == date(2026, 8, 25)


def test_first_eval_log_is_first_write_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(tonight, "LOG_PATH", tmp_path / "tonight.jsonl")
    r = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY)
    first = tonight.log_first_eval(r)
    assert first["verdict"] == "CLEAN"
    # A later, different evaluation must NOT overwrite the decision-time record.
    r2 = tonight.evaluate(_tape(p1400=24420.0), _vix(), TODAY, NOW, NEXT, EXPIRY)
    standing = tonight.log_first_eval(r2)
    assert standing["verdict"] == "CLEAN"
    assert len((tmp_path / "tonight.jsonl").read_text().splitlines()) == 1


def test_provisional_never_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(tonight, "LOG_PATH", tmp_path / "tonight.jsonl")
    tape = _tape()
    del tape[TODAY].bars[(15, 0)]
    r = tonight.evaluate(tape, _vix(), TODAY,
                         datetime(2026, 8, 20, 14, 30, tzinfo=IST), NEXT, EXPIRY)
    assert tonight.log_first_eval(r) is None
    assert not (tmp_path / "tonight.jsonl").exists()
