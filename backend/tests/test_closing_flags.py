"""The pre-registered filter flags: definitions frozen, unknowns never pass.

The whole point of registering these is that the live scoreboard's count only
means something if the rules cannot drift. So the registration date, the
mid-range band, and the strictly-True kept semantics are all pinned.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing.calendar import IST
from app.closing import flags as filters


def test_registration_is_frozen():
    """Editing either constant resets the live ledger — pinned so it cannot
    happen silently."""
    assert filters.REGISTERED_ON == date(2026, 8, 19)
    assert (filters.MID_LO, filters.MID_HI) == (0.35, 0.65)
    assert filters.MIN_LIVE_SAMPLE == 30


def _trade(d="2026-08-18", vix_in=12.0, signal=24300.0, **kw):
    t = {"date": d, "month": d[:7], "vix_in": vix_in, "signal_price": signal,
         "net_pct": 0.0, "net_rs": 0.0}
    t.update(kw)
    return t


def _spine(d: date, bars):
    """bars: [(hh, mm, high, low)] -> minimal spine frame for one day."""
    rows = []
    for hh, mm, h, l in bars:
        ts = int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp())
        rows.append((ts, l, h, l, l, 0.0))
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol_proxy"])


def _vix(d: date, v0915, v1500, prev_day=None, prev_close=None):
    """A missing bar is genuinely absent from the frame (the real store has no
    NULL rows), so None values are skipped rather than written as NaN."""
    rows = []
    if prev_day is not None:
        ts = int(datetime(prev_day.year, prev_day.month, prev_day.day, 15, 25,
                          tzinfo=IST).timestamp())
        rows.append((ts, prev_close, prev_close, prev_close, prev_close))
    for (hh, mm), v in (((9, 15), v0915), ((15, 0), v1500)):
        if v is None:
            continue
        ts = int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp())
        rows.append((ts, v, v, v, v))
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])


D = date(2026, 8, 18)
PREV = date(2026, 8, 17)
SPINE = _spine(D, [(9, 15, 24400, 24200), (15, 0, 24400, 24200)])


def test_vol_expand_fires_on_either_leg_and_only_above_zero():
    # VIX 12.0 at 15:00, prev close 11.5 -> vs_prev +0.5 -> True
    t = _trade(vix_in=12.0)
    filters.annotate([t], SPINE, _vix(D, 12.3, 12.0, PREV, 11.5))
    assert t["f_vol_expand"] is True and t["vix_vs_prev"] == 0.5
    # both legs negative -> False
    t = _trade(vix_in=11.0)
    filters.annotate([t], SPINE, _vix(D, 11.4, 11.0, PREV, 11.5))
    assert t["f_vol_expand"] is False
    # exactly zero on both -> NOT expansion (strictly above)
    t = _trade(vix_in=11.5)
    filters.annotate([t], SPINE, _vix(D, 11.5, 11.5, PREV, 11.5))
    assert t["f_vol_expand"] is False
    # day leg alone can fire it: below prev close but above its own open
    t = _trade(vix_in=11.4)
    filters.annotate([t], SPINE, _vix(D, 11.1, 11.4, PREV, 11.5))
    assert t["f_vol_expand"] is True


def test_missing_vix_context_is_unknown_not_pass():
    t = _trade(vix_in=12.0)
    filters.annotate([t], SPINE, _vix(D, None, 12.0))   # no 09:15, no prev day
    assert t["f_vol_expand"] is None
    assert t["f_all_pass"] is None


def test_midrange_band_edges():
    vix = _vix(D, 11.0, 12.0, PREV, 11.5)
    # range 24200-24400: 0.5 of range = 24300 -> mid-range -> fails
    t = _trade(signal=24300.0)
    filters.annotate([t], SPINE, vix)
    assert t["f_midrange"] is False and t["range_pos"] == 0.5
    # exactly at the 0.35 edge (24270) -> band is OPEN, edge passes
    t = _trade(signal=24270.0)
    filters.annotate([t], SPINE, vix)
    assert t["f_midrange"] is True
    # near the low -> passes
    t = _trade(signal=24210.0)
    filters.annotate([t], SPINE, vix)
    assert t["f_midrange"] is True


def test_ladder_rungs_partition_and_unknowns_never_climb():
    trades = []
    for i, (ve, mr) in enumerate([(True, True), (True, False), (False, True),
                                  (None, True), (True, None)]):
        trades.append({"date": f"2026-08-{10+i:02d}", "month": "2026-08",
                       "net_pct": 1.0, "net_rs": 100.0,
                       "f_vol_expand": ve, "f_midrange": mr})
    rungs = filters.ladder(trades)
    assert rungs[0]["kept"]["n"] == 5
    assert rungs[1]["kept"]["n"] == 3          # the three True vol_expand
    assert rungs[1]["removed"]["n"] == 2       # False AND None both fall out
    assert rungs[1]["unknown_n"] == 1
    # rung 2 filters the rung-1 keeps (T,T), (T,F), (T,None): only (T,T) climbs
    assert rungs[2]["kept"]["n"] == 1
    assert rungs[2]["removed"]["n"] == 2
    assert rungs[2]["unknown_n"] == 1


def test_live_scoreboard_counts_only_after_registration():
    mk = lambda d, ap: {"date": d, "month": d[:7], "net_pct": 5.0, "net_rs": 100.0,
                        "f_all_pass": ap, "f_vol_expand": ap, "f_midrange": ap}
    trades = [mk("2026-08-18", True),   # before/on registration -> not live
              mk("2026-08-19", True),   # ON the date -> not live (strictly after)
              mk("2026-08-20", True),
              mk("2026-08-21", False)]
    sb = filters.live_scoreboard(trades)
    assert sb["live_nights"] == 2
    assert sb["filtered_nights"] == 1
    assert sb["filters_fail"]["n"] == 1
    assert sb["verdict_due"] == 29
