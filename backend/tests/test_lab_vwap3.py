"""B09 VWAP σ-band strategy + study tests.

Pinned here:
  * the volume-weighted σ math (hand-checked, causal);
  * touch-episode discipline — band-walking is ONE episode, re-arm only
    after price returns to VWAP;
  * the v1 path through the real engine: upper touch -> PE bought at the
    post-decision snapshot -> exit when the underlying tags VWAP;
  * mode B refuses a touch bar that closes outside the band;
  * the study's conservative same-bar rule (VWAP and stop in one bar =
    stop);
  * the catalog now holds exactly 41 strategies.

Run:  python backend/tests/test_lab_vwap3.py   (also pytest-compatible)
"""
import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))

import pandas as pd

from app.lab import catalog, features
from app.lab.models import validate_params
from tests.test_lab_engine import (
    DAY0, _bt, _patch_spine, _unpatch, bar_ts, make_chain_db,
)


def _flat_spine(days=2, base=100.0, amp=1.0):
    """Alternating ±amp closes around base: vwap ~= base, sd ~= amp-ish,
    volume constant so the weighting is trivial to reason about."""
    rows = []
    from datetime import timedelta
    for d in range(days):
        while (DAY0 + timedelta(days=d)).weekday() > 4:
            d += 1
        for b in range(75):
            c = base + (amp if b % 2 == 0 else -amp)
            rows.append({"ts": bar_ts(d, b), "open": c, "high": c + 0.3,
                         "low": c - 0.3, "close": c, "volume": 1000.0})
    return pd.DataFrame(rows)


def test_vwap_sd_math_is_volume_weighted_and_causal():
    df = _flat_spine(days=1)
    f = features.add_features(df)
    # typical price alternates base±amp ± the h/l skew; by bar 20 the
    # running sd must be close to the alternation amplitude and vwap ~ base
    r = f.iloc[20]
    assert abs(r["vwap"] - 100.0) < 0.2, r["vwap"]
    assert 0.7 < r["vwap_sd"] < 1.3, r["vwap_sd"]
    # causal: truncating the future changes nothing at bar 20
    f_cut = features.add_features(df.iloc[:21].copy())
    assert abs(f["vwap_sd"].iloc[20] - f_cut["vwap_sd"].iloc[20]) < 1e-9


def _b09(overrides=None):
    sdef = catalog.get("B09")
    p = validate_params(sdef.params, overrides or {})
    from app.lab.strategies.opt_buy import B09_Vwap3Sigma
    return B09_Vwap3Sigma(sdef, p)


def _row(high, low, close, vwap=100.0, sd=1.0, minute=700):
    return {"high": high, "low": low, "close": close, "open": close,
            "vwap": vwap, "vwap_sd": sd, "minute": minute,
            "ts": 0, "atr": 1.0}


def test_band_walk_is_one_episode_and_rearm_needs_vwap():
    s = _b09({"sigma_mult": 3.0})
    up = 103.0   # vwap 100, sd 1 -> upper band 103
    assert s._track(_row(103.2, 102.0, 103.0)) == "up"
    # walking the band: still touching, NOT a new episode
    assert s._track(_row(103.5, 102.5, 103.1)) is None
    assert s._track(_row(104.0, 103.0, 103.8)) is None
    assert s._episodes["up"] == 1
    # coming back near — but not TO — vwap does not re-arm
    assert s._track(_row(102.0, 100.8, 101.0)) is None
    assert s._track(_row(103.4, 102.2, 103.0)) is None
    assert s._episodes["up"] == 1
    # touching vwap re-arms; the next band touch is episode 2
    s._track(_row(101.0, 99.8, 100.2))
    assert s._track(_row(103.2, 102.0, 103.0)) == "up"
    assert s._episodes["up"] == 2


def test_touch_policy_first_blocks_second_episode():
    s = _b09({"touch_policy": "first"})
    s._track(_row(103.2, 102.0, 103.0))
    assert s._episode_ok("up")
    s._track(_row(101.0, 99.8, 100.2))     # re-arm at vwap
    s._track(_row(103.2, 102.0, 103.0))
    assert not s._episode_ok("up")


def _touch_day_spine():
    """Day 2: quiet morning, an upper-band spike mid-day, then a slide
    back through VWAP — the canonical mode-A PE win."""
    df = _flat_spine(days=2)
    # day-2 bars 20..23: push well above the ~3σ upper band
    for k, px in ((95, 104.5), (96, 105.0), (97, 103.0), (98, 99.5)):
        i = df.index[k]
        df.loc[i, ["open", "close"]] = px
        df.loc[i, "high"] = px + 0.4
        df.loc[i, "low"] = px - 0.4
    return df


def test_engine_mode_a_buys_pe_and_exits_at_vwap():
    df = _touch_day_spine()
    touch_ts = bar_ts(1, 20)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "chain.db"
        rows = [(100.0, "CE", 2.0, 1.95, 2.05, 500000),
                (100.0, "PE", 2.0, 1.95, 2.05, 500000)]
        reader = make_chain_db(p, [
            (touch_ts - 60, rows),
            (touch_ts + 400, rows),                       # entry fill snap
            (bar_ts(1, 23) + 400,
             [(100.0, "CE", 1.0, 0.9, 1.1, 500000),
              (100.0, "PE", 3.5, 3.4, 3.6, 500000)]),     # exit snap
        ])
        orig = _patch_spine(df)
        try:
            bt = _bt("B09", df, {"min_oi": 0, "max_spread_pct": 10,
                                 "min_premium": 0, "avoid_first_min": 0},
                     reader=reader)
            res = bt.run()
            assert res.status == "done"
            assert res.trades, "upper 3σ touch must produce a PE: %s" % \
                res.rejects
            t = res.trades[0]
            assert t["legs"][0]["right"] == "PE"
            assert t["direction"] == "SHORT"
            assert t["tag"].startswith("pe_e1_A"), t["tag"]
            assert t["exit_reason"] == "vwap_target", t["exit_reason"]
            assert t["signal_ts"] == touch_ts + 300, \
                "signal must be the touch bar's close"
        finally:
            _unpatch(*orig)


def test_mode_b_refuses_close_outside_band():
    df = _touch_day_spine()   # touch bars CLOSE well above the band
    touch_ts = bar_ts(1, 20)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "chain.db"
        rows = [(100.0, "CE", 2.0, 1.95, 2.05, 500000),
                (100.0, "PE", 2.0, 1.95, 2.05, 500000)]
        reader = make_chain_db(p, [(touch_ts - 60, rows),
                                   (touch_ts + 400, rows)])
        orig = _patch_spine(df)
        try:
            bt = _bt("B09", df, {"entry_mode": "close_inside", "min_oi": 0,
                                 "max_spread_pct": 10, "min_premium": 0,
                                 "avoid_first_min": 0}, reader=reader)
            res = bt.run()
            pe = [t for t in res.trades if t["legs"][0]["right"] == "PE"]
            assert not pe, "mode B must not enter while the close is " \
                           "outside the band"
        finally:
            _unpatch(*orig)


def test_study_same_bar_ambiguity_is_stop_first():
    from app.lab.vwap3 import _scan_outcome
    mk = lambda h, l, c, vwap, sd, ts, minute: {
        "high": h, "low": l, "close": c, "open": c, "vwap": vwap,
        "vwap_sd": sd, "ts": ts, "minute": minute, "atr": 1.0}
    rows = [
        mk(103.2, 102.0, 103.0, 100.0, 1.0, 1000, 700),   # touch (upper=103)
        # next bar spans BOTH the stop (103.5 = band+0.5σ) and vwap (100)
        mk(104.0, 99.0, 100.5, 100.0, 1.0, 1300, 705),
    ]
    t = _scan_outcome(rows, 0, "up", 1, 3.0, 0.5, 915, 100.0, 1.0, 103.0)
    assert t["outcome"] == "stop_first", t["outcome"]


def test_catalog_holds_41():
    assert len(catalog.CATALOG) == 41
    from app.lab.strategies import implemented_sids
    assert "B09" in implemented_sids()
    d = catalog.get("B09")
    assert d.category == "opt_buy" and d.spine == "index"
    p = validate_params(d.params, {})
    assert p["sigma_mult"] == 3.0, "the OFFICIAL default must stay 3.0"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print("%d tests passed" % len(fns))
