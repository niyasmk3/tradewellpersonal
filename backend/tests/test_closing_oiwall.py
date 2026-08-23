"""The OI-wall shadow signal: frozen math and states, both bhavcopy layouts,
the cache round-trip, the unknown-never-passes rule, and that it rides the
card without touching the verdict."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import oiwall, tonight
from app.closing.calendar import IST
from app.closing.study import Day


def _chain(ce: dict, pe: dict) -> dict:
    out = {}
    out.update({(float(s), "CE"): oi for s, oi in ce.items()})
    out.update({(float(s), "PE"): oi for s, oi in pe.items()})
    return out


CHAIN = _chain({24200: 1e5, 24300: 3e5, 24500: 9e5, 24800: 2e5},   # call wall 24500
               {23900: 8e5, 24000: 4e5, 24200: 1e5, 24300: 5e4})   # put wall 23900


def test_walls_states_and_pcr():
    assert oiwall.SPAN == 500.0 and oiwall.ROAD_PTS == 150.0 and oiwall.REGISTERED_ON == "2026-08-22"
    w = oiwall.walls(CHAIN, 24250.0, "CE")
    assert w["ce_wall"] == 24500 and w["pe_wall"] == 23900
    assert w["ahead_pts"] == 250.0 and w["state"] == "ROAD"
    assert abs(w["pcr"] - (8e5 + 4e5 + 1e5 + 5e4) / (1e5 + 3e5 + 9e5)) < 1e-3   # 24800 is outside +/-500 of 24250
    w = oiwall.walls(CHAIN, 24250.0, "PE")
    assert w["ahead_pts"] == 350.0 and w["state"] == "ROAD"
    w = oiwall.walls(CHAIN, 24420.0, "CE")            # call wall 80 pts up
    assert w["ahead_pts"] == 80.0 and w["state"] == "CAPPED"
    w = oiwall.walls(CHAIN, 24550.0, "CE")            # print already past the wall
    assert w["ahead_pts"] == -50.0 and w["state"] == "BEHIND"
    w = oiwall.walls(CHAIN, 24250.0, None)            # flat body: walls known, no state
    assert w["ce_wall"] == 24500 and w["state"] is None and w["ahead_pts"] is None


def test_walls_unknown_when_a_side_is_empty():
    assert oiwall.walls(_chain({24500: 1.0}, {}), 24250.0, "CE") is None
    assert oiwall.walls(CHAIN, 30000.0, "CE") is None  # nothing within the span


def test_pick_expiry_prefers_held_then_nearest_later():
    chains = {"2026-08-25": {}, "2026-09-01": {}, "2026-08-18": {}}
    assert oiwall.pick_expiry(chains, date(2026, 8, 25), date(2026, 8, 21)) == "2026-08-25"
    assert oiwall.pick_expiry(chains, date(2026, 9, 8), date(2026, 8, 21)) == "2026-08-25"   # not listed -> nearest after
    assert oiwall.pick_expiry({}, date(2026, 8, 25), date(2026, 8, 21)) is None


UDIFF = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4
2026-08-21,2026-08-21,FO,NSE,IDO,1,,NIFTY,,2026-08-25,2026-08-25,24500.00,CE,NIFTY26AUG24500CE,1,1,1,1,1,1,24234,1,900000,10000,1,1,1,F1,65,,,,,
2026-08-21,2026-08-21,FO,NSE,IDO,2,,NIFTY,,2026-08-25,2026-08-25,24000.00,PE,NIFTY26AUG24000PE,1,1,1,1,1,1,24234,1,400000,-5000,1,1,1,F1,65,,,,,
2026-08-21,2026-08-21,FO,NSE,IDO,3,,BANKNIFTY,,2026-08-25,2026-08-25,57000.00,CE,BANKNIFTY26AUG57000CE,1,1,1,1,1,1,57700,1,123,0,1,1,1,F1,35,,,,,
2026-08-21,2026-08-21,FO,NSE,IDF,4,,NIFTY,,2026-08-25,2026-08-25,0,XX,NIFTY26AUGFUT,1,1,1,24293,1,1,24234,1,10834655,0,1,1,1,F1,65,,,,,
"""
LEGACY = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,
OPTIDX,NIFTY,25-Jan-2024,21500,CE,1,1,1,1,1,1,1,250000,1000,02-JAN-2024,
OPTIDX,NIFTY,25-Jan-2024,21000,PE,1,1,1,1,1,1,1,300000,2000,02-JAN-2024,
FUTIDX,NIFTY,25-Jan-2024,0,XX,1,1,1,21700,21700,1,1,1,1,02-JAN-2024,
"""


def test_parsers_keep_only_nifty_options_with_close_oi():
    u = oiwall.parse_udiff(UDIFF)
    assert u == {"2026-08-25": {(24500.0, "CE"): 900000.0, (24000.0, "PE"): 400000.0}}
    l = oiwall.parse_legacy(LEGACY)
    assert l == {"2024-01-25": {(21500.0, "CE"): 250000.0, (21000.0, "PE"): 300000.0}}


def test_cache_round_trip_and_fetch_failure_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(oiwall, "CACHE_DIR", tmp_path / "cache")
    chains = oiwall.parse_udiff(UDIFF)
    oiwall._to_cache(date(2026, 8, 21), chains)
    assert oiwall._from_cache(date(2026, 8, 21)) == chains
    # No cache + dead archive -> None, no exception, nothing cached.
    monkeypatch.setattr(oiwall, "fetch_bhavcopy", lambda session, timeout=20.0: None)
    assert oiwall.prev_close_chains(date(2026, 8, 20)) is None
    assert not (tmp_path / "cache" / "2026-08-20.json").exists()
    # Cached session never hits the network.
    monkeypatch.setattr(oiwall, "fetch_bhavcopy", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert oiwall.prev_close_chains(date(2026, 8, 21)) == chains
    # A failed session is not retried inside the backoff window (the card is polled).
    assert oiwall.prev_close_chains(date(2026, 8, 20)) is None      # would raise if it hit the network
    oiwall._failed_at.clear()


def _day(d: date, bars: dict) -> Day:
    day = Day(d)
    ts0 = int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())
    for (h, m), (o, hi, lo, c) in bars.items():
        day.bars[(h, m)] = (o, hi, lo, c, ts0 + h * 3600 + m * 60)
    return day


def test_annotate_reads_previous_session_and_unknown_stays_none():
    d0, d1, d2 = date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)
    days = {d: _day(d, {(15, 0): (24250.0, 24260.0, 24240.0, 24255.0)}) for d in (d0, d1, d2)}
    backfill = {d1.isoformat(): {"2026-08-25": CHAIN}}          # only d1's close is known
    trades = [{"date": d2.isoformat(), "expiry": "2026-08-25", "signal_price": 24250.0, "direction": "CE"},
              {"date": d1.isoformat(), "expiry": "2026-08-25", "signal_price": 24250.0, "direction": "PE"},
              {"date": d0.isoformat(), "expiry": "2026-08-25", "signal_price": 24250.0, "direction": "CE"}]
    oiwall.annotate(trades, days, backfill)
    assert trades[0]["f_oi_state"] == "ROAD" and trades[0]["oi_ahead_pts"] == 250.0 and trades[0]["oi_ce_wall"] == 24500
    assert trades[1]["f_oi_state"] is None and trades[1]["oi_pcr"] is None     # d0's close not in the backfill
    assert trades[2]["f_oi_state"] is None                                      # first session: no prior


def test_summary_cells_and_coverage():
    split = date(2026, 1, 1)
    tr = [{"date": "2025-06-01", "card_verdict": "CLEAN", "f_oi_state": "ROAD", "net_pct": 30.0, "net_rs": 300},
          {"date": "2025-06-02", "card_verdict": "CLEAN", "f_oi_state": "CAPPED", "net_pct": -20.0, "net_rs": -200},
          {"date": "2025-06-03", "card_verdict": "FLAGGED", "f_oi_state": "ROAD", "net_pct": -10.0, "net_rs": -100},
          {"date": "2026-03-01", "card_verdict": "CLEAN", "f_oi_state": None, "net_pct": 5.0, "net_rs": 50}]
    s = oiwall.summary(tr, split)
    w = s["windows"]["in_sample_2y"]
    assert w["clean_road"]["n"] == 1 and w["clean_capped"]["n"] == 1 and w["clean_behind"]["n"] == 0 and w["flagged_road"]["n"] == 1
    assert s["windows"]["holdout_1y"]["clean_road"]["n"] == 0          # unknown counts nowhere
    assert s["coverage"] == {"stamped": 3, "ledger": 4} and s["road_pts"] == 150.0


def test_card_carries_oi_wall_as_shadow_without_touching_verdict():
    from tests.test_closing_tonight import EXPIRY, NEXT, NOW, TODAY, _tape, _vix
    chains = {EXPIRY.isoformat(): CHAIN}
    r = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY, chains)
    v = r["values"]
    assert v["direction"] == "CE" and v["oi_ce_wall"] == 24500 and v["oi_pe_wall"] == 23900
    assert v["oi_state"] == "CAPPED" and v["oi_ahead_pts"] == 100.0           # print 24400, wall 24500
    assert r["verdict"] == "CLEAN"                                             # a capped night is still CLEAN
    assert [c["key"] for c in r["checks"]] == ["lasthr", "volexp", "midrange", "bridge", "monthend"]
    # No chain -> unknown, nothing else changes.
    r2 = tonight.evaluate(_tape(), _vix(), TODAY, NOW, NEXT, EXPIRY)
    assert r2["values"]["oi_state"] is None and r2["verdict"] == "CLEAN"
