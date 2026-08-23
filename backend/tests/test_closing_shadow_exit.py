"""The 10:45 shadow exit: identical fill/charge mechanics at the later print,
paired summaries, and the live real-quote capture windows (first-per-night)."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import shadow_exit
from app.closing.calendar import IST
from app.closing.pricing import IvCurve, OptionModel
from app.closing.study import Day, StudyConfig
from app.paper.charges import charges

NIGHT, EXIT = date(2026, 8, 20), date(2026, 8, 21)
EXPIRY = date(2026, 8, 25)


def _day(d, bars):
    day = Day(d)
    ts0 = int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())
    for (h, m), (o, hi, lo, c) in bars.items():
        day.bars[(h, m)] = (o, hi, lo, c, ts0 + h * 3600 + m * 60)
    return day


class _Vix:
    def __init__(self, v=12.0):
        self.v = v

    def at(self, ts):
        return self.v


def _trade(direction="CE", strike=24300.0, fill_in=100.0, mid_in=99.88, net_rs=0.0):
    return {"date": NIGHT.isoformat(), "exit_date": EXIT.isoformat(),
            "expiry": EXPIRY.isoformat(), "direction": direction, "strike": strike,
            "entry_spot": 24310.0, "fill_in": fill_in, "mid_in": mid_in,
            "qty": 65, "net_rs": net_rs, "net_pct": 0.0, "card_verdict": "CLEAN"}


def test_annotate_prices_1045_with_study_mechanics():
    days = {EXIT: _day(EXIT, {(9, 50): (24350.0, 24360.0, 24340.0, 24355.0),
                              (10, 45): (24420.0, 24430.0, 24410.0, 24425.0)})}
    model, cfg = OptionModel(IvCurve(None)), StudyConfig()
    t = _trade()
    shadow_exit.annotate([t], days, _Vix(), model, cfg)
    assert t["x1045_exit_spot"] == 24420.0
    assert t["x1045_signed_move_pts"] == 110.0
    # Reproduce the fill/charge arithmetic independently.
    mid = model.premium(24420.0, 24300.0, days[EXIT].bar((10, 45))[4], EXPIRY, True, 12.0)
    fill = round(max(mid * (1 - cfg.exit_spread_pct / 2), 0.05), 2)
    net = (fill - 100.0) * 65 - charges(100.0, fill, 65, 2)
    assert t["x1045_fill_out"] == fill
    assert abs(t["x1045_net_rs"] - round(net, 2)) < 0.01
    assert t["x1045_delta_rs"] == t["x1045_net_rs"]   # net_rs was 0


def test_missing_1045_bar_stays_unknown():
    days = {EXIT: _day(EXIT, {(9, 50): (24350.0, 24360.0, 24340.0, 24355.0)})}
    t = _trade()
    shadow_exit.annotate([t], days, _Vix(), OptionModel(IvCurve(None)), StudyConfig())
    assert t["x1045_net_rs"] is None and t["x1045_delta_rs"] is None


def test_summary_pairs_only_known_nights(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_exit, "QUOTES_PATH", tmp_path / "q.jsonl")
    split = date(2026, 1, 1)
    a = {"date": "2026-03-01", "card_verdict": "CLEAN", "net_pct": 10.0, "net_rs": 500.0,
         "x1045_net_pct": 20.0, "x1045_net_rs": 900.0}
    b = {"date": "2026-03-02", "card_verdict": "CLEAN", "net_pct": -5.0, "net_rs": -200.0,
         "x1045_net_pct": None, "x1045_net_rs": None}            # unknown -> excluded from BOTH
    c = {"date": "2025-03-02", "card_verdict": "FLAGGED", "net_pct": -30.0, "net_rs": -1000.0,
         "x1045_net_pct": -40.0, "x1045_net_rs": -1300.0}
    s = shadow_exit.summary([a, b, c], split)
    h = s["windows"]["holdout_1y"]
    assert h["clean_0950"]["n"] == 1 and h["clean_1045"]["n"] == 1
    assert h["clean_0950"]["total_rs"] == 500 and h["clean_1045"]["total_rs"] == 900
    assert h["unknown_n"] == 1
    assert s["windows"]["in_sample_2y"]["flagged_1045"]["total_rs"] == -1300
    assert s["registered_on"] == "2026-08-22" and s["live"]["n"] == 0


class _Kite:
    def __init__(self):
        self.calls = []

    def quote(self, symbols):
        self.calls.append(symbols)
        return {symbols[0]: {"last_price": 101.0, "oi": 10,
                             "depth": {"buy": [{"price": 100.0}], "sell": [{"price": 102.0}]}}}


def _cards(night=NIGHT, nxt=EXIT):
    return [{"date": night.isoformat(), "verdict": "CLEAN", "red": [],
             "values": {"direction": "CE", "strike": 24300.0, "expiry": EXPIRY.isoformat(),
                        "next_trading_day": nxt.isoformat()}}]


def test_capture_windows_route_to_the_right_card(tmp_path, monkeypatch):
    from app.closing import tonight
    monkeypatch.setattr(tonight, "logged_cards", lambda: _cards())
    monkeypatch.setattr(shadow_exit, "_tradingsymbol", lambda k, e, s, d: "NIFTY26AUG24300CE")
    kite, p = _Kite(), tmp_path / "q.jsonl"
    at = lambda d, h, m: datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    assert shadow_exit.capture_quote(kite, at(NIGHT, 15, 6), p) == "entry"     # tonight's card
    assert shadow_exit.capture_quote(kite, at(NIGHT, 15, 7), p) is None        # first wins
    assert shadow_exit.capture_quote(kite, at(NIGHT, 14, 0), p) is None        # no window
    assert shadow_exit.capture_quote(kite, at(EXIT, 9, 51), p) == "exit_0950"  # last night's
    assert shadow_exit.capture_quote(kite, at(EXIT, 10, 46), p) == "exit_1045"
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["entry", "exit_0950", "exit_1045"]
    assert all(r["night"] == NIGHT.isoformat() and r["mid"] == 101.0 for r in rows)
    assert shadow_exit.capture_quote(None, at(EXIT, 9, 51), p) is None         # no kite, no row


def test_live_summary_needs_all_three_quotes(tmp_path):
    p = tmp_path / "q.jsonl"
    rows = [{"night": "2026-08-20", "kind": "entry", "mid": 100.0, "verdict": "CLEAN", "direction": "CE"},
            {"night": "2026-08-20", "kind": "exit_0950", "mid": 110.0},
            {"night": "2026-08-20", "kind": "exit_1045", "mid": 125.0},
            {"night": "2026-08-21", "kind": "entry", "mid": 90.0}]          # incomplete
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    s = shadow_exit.live_summary([{"date": "2026-08-20", "net_pct": 8.0, "x1045_net_pct": 21.0}], p)
    assert s["n"] == 1
    r = s["rows"][0]
    assert r["real_0950_pct"] == 10.0 and r["real_1045_pct"] == 25.0
    assert r["model_1045_pct"] == 21.0 and s["nights_1045_better"] == 1
