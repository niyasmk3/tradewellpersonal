"""The US-event shadow flag: window semantics, calendar coverage (unknown
never passes), the ledger stamp, and that it rides the card without touching
the verdict or the tier."""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import events, tonight


def test_calendar_has_the_anchor_dates():
    data = events.load()
    assert {2023, 2024, 2025, 2026} <= data["years"]
    assert events.tonight(date(2026, 7, 29)) == ["FOMC"]         # Fed decision day
    assert events.tonight(date(2026, 8, 12)) == ["CPI"]
    assert events.tonight(date(2026, 8, 7)) == ["NFP"]
    assert events.tonight(date(2025, 10, 24)) == ["CPI"]         # the shutdown-shifted Sept CPI
    assert events.tonight(date(2025, 11, 20)) == ["NFP"]
    assert events.tonight(date(2026, 8, 24)) == []               # Monday 24-Aug: nothing scheduled
    assert events.REGISTERED_ON == "2026-08-23"


def test_unknown_year_is_none_not_false():
    assert events.tonight(date(2027, 1, 13)) is None
    assert events.tonight(date(2022, 6, 10)) is None
    data = {"years": {2026}, "events": {"2026-08-12": ["CPI", "PPI"]}}
    assert events.tonight(date(2026, 8, 12), data) == ["CPI"]    # unknown kinds dropped
    assert events.tonight(date(2027, 8, 12), data) is None


def test_annotate_stamps_flag_and_unknown():
    tr = [{"date": "2026-07-29"}, {"date": "2026-08-24"}, {"date": "2027-03-03"}]
    events.annotate(tr)
    assert tr[0]["events"] == ["FOMC"] and tr[0]["f_event"] is True
    assert tr[1]["events"] == [] and tr[1]["f_event"] is False
    assert tr[2]["events"] is None and tr[2]["f_event"] is None


def test_summary_cells_carry_magnitude():
    split = date(2026, 1, 1)
    tr = [{"date": "2025-06-01", "card_verdict": "CLEAN", "f_event": True, "events": ["NFP"], "net_pct": 50.0, "net_rs": 500, "signed_move_pts": 200.0},
          {"date": "2025-06-02", "card_verdict": "CLEAN", "f_event": False, "events": [], "net_pct": -10.0, "net_rs": -100, "signed_move_pts": -20.0},
          {"date": "2025-06-03", "card_verdict": "FLAGGED", "f_event": True, "events": ["CPI"], "net_pct": -30.0, "net_rs": -300, "signed_move_pts": 90.0},
          {"date": "2027-01-05", "card_verdict": "CLEAN", "f_event": None, "events": None, "net_pct": 5.0, "net_rs": 50, "signed_move_pts": 10.0}]
    s = events.summary(tr, split)
    w = s["windows"]["in_sample_2y"]
    assert w["clean_event"]["n"] == 1 and w["clean_event"]["abs_move_mean"] == 200.0 and w["clean_event"]["idx_continued_pct"] == 100.0
    assert w["clean_no_event"]["n"] == 1 and w["flagged_event"]["n"] == 1
    assert s["windows"]["holdout_1y"]["clean_event"]["n"] == 0           # unknown counts nowhere
    assert s["by_kind_clean_3y"]["NFP"]["n"] == 1 and s["by_kind_clean_3y"]["FOMC"]["n"] == 0
    assert s["coverage"] == {"stamped": 3, "ledger": 4}


def test_card_carries_event_flag_without_touching_verdict_or_tier(monkeypatch):
    from tests.test_closing_tonight import EXPIRY, NEXT, NOW, TODAY, _tape, _vix
    # TODAY is 2026-08-20 (no event). Pretend it were an FOMC night.
    monkeypatch.setattr(events, "_cache", {"years": {2026}, "events": {TODAY.isoformat(): ["FOMC"]}})
    r = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY)
    assert r["values"]["events"] == ["FOMC"] and r["values"]["event_tonight"] is True
    assert r["verdict"] == "CLEAN" and r["tier"] is None                 # OI unknown -> untiered, unchanged
    monkeypatch.setattr(events, "_cache", {"years": set(), "events": {}})
    r2 = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY)
    assert r2["values"]["events"] is None and r2["values"]["event_tonight"] is None
    assert r2["verdict"] == "CLEAN"
