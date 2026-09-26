"""Strategy Lab engine tests — the honesty properties, pinned.

What is pinned here and why it matters:
  * entries fill on the NEXT bar's open, never the signal bar (look-ahead);
  * same-bar stop+target resolves to the stop (conservative);
  * no fills across a day boundary;
  * option legs fill from the first chain snapshot AFTER the decision,
    at ask+slip / bid-slip, and a whole order dies if ONE leg can't fill;
  * strikes that were never listed can't be traded;
  * the cost model agrees with the audited app/paper/charges.py rates;
  * lot sizes come from the era of the trade, not today;
  * runs and day-ledgers are immutable in the store;
  * features are causal (truncating the future changes nothing).

Run:  python backend/tests/test_lab_engine.py   (also pytest-compatible)
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))

import pandas as pd

from app.lab import catalog, data as data_mod, features
from app.lab import costs as costs_mod
from app.lab.data import ChainReader
from app.lab.engine import Backtester
from app.lab.fills import lot_size_for
from app.lab.models import validate_params
from app.market.calendar import IST
from datetime import datetime, timedelta

DAY0 = datetime(2026, 9, 14, tzinfo=IST)   # a Monday


def bar_ts(day_idx: int, bar_idx: int) -> int:
    d = DAY0 + timedelta(days=day_idx)
    return int(d.replace(hour=9, minute=15).timestamp()) + bar_idx * 300


def make_spine(days=3, bars_per_day=75, base=24000.0, drift=0.0,
               with_oi=True) -> pd.DataFrame:
    rows = []
    px = base
    for d in range(days):
        # skip weekends so IST-day logic sees real trading days
        while (DAY0 + timedelta(days=d)).weekday() > 4:
            d += 1
        for b in range(bars_per_day):
            ts = bar_ts(d, b)
            o = px
            h, l = px + 6, px - 6
            c = px + drift
            rows.append({"ts": ts, "open": o, "high": h, "low": l,
                         "close": c, "volume": 1000.0,
                         **({"oi": 100000.0 + 100 * b} if with_oi else {})})
            px = c
    return pd.DataFrame(rows)


def make_chain_db(path: Path, snaps: list) -> ChainReader:
    """snaps: list of (ts, [(strike, right, ltp, bid, ask, oi)]) with a fixed
    expiry."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chain_snap (ts INTEGER NOT NULL, token INTEGER NOT "
        "NULL, tick_ts INTEGER, symbol TEXT, expiry TEXT, strike REAL, "
        "right TEXT, ltp REAL, bid REAL, ask REAL, oi REAL, volume REAL, "
        "iv REAL, PRIMARY KEY (ts, token))")
    token = 1000
    for ts, rows in snaps:
        for (strike, right, ltp, bid, ask, oi) in rows:
            token += 1
            conn.execute(
                "INSERT INTO chain_snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, token, ts, "NIFTY", "2026-09-15", strike, right, ltp,
                 bid, ask, oi, 100.0, 15.0))
    conn.commit()
    conn.close()
    return ChainReader(path)


class _EmptyReader:
    def available(self):
        return False

    def snap_ts_list(self, refresh=False):
        return []

    def snap_at_or_before(self, ts, max_age_s=900):
        return None

    def snap_after(self, ts, ttl_s=900):
        return None

    def _conn(self):
        raise RuntimeError("no chain db")


def _patch_spine(df: pd.DataFrame):
    """Point the engine's futures/index loaders at a synthetic frame."""
    class _Stub:
        def load_frame(self, from_ts=None, to_ts=None):
            d = df
            if from_ts is not None:
                d = d[d["ts"] >= from_ts]
            if to_ts is not None:
                d = d[d["ts"] <= to_ts]
            return d.reset_index(drop=True)
    data_mod.futures_store, orig = _Stub(), data_mod.futures_store
    orig_idx = data_mod.load_index_frame
    data_mod.load_index_frame = lambda from_ts=None, to_ts=None: \
        _Stub().load_frame(from_ts, to_ts)
    orig_vix = data_mod.load_vix_daily
    data_mod.load_vix_daily = lambda: pd.DataFrame(columns=["ts", "close"])
    return orig, orig_idx, orig_vix


def _unpatch(orig, orig_idx, orig_vix):
    data_mod.futures_store = orig
    data_mod.load_index_frame = orig_idx
    data_mod.load_vix_daily = orig_vix


def _bt(sid: str, df: pd.DataFrame, overrides=None, reader=None,
        capital=1000000.0) -> Backtester:
    sdef = catalog.get(sid)
    p = validate_params(sdef.params, overrides or {})
    return Backtester(sdef, p, sdef.timeframe, int(df["ts"].iloc[0]),
                      int(df["ts"].iloc[-1]) + 300, capital,
                      chain_reader=reader if reader is not None
                      else _EmptyReader())


def test_catalog_integrity():
    assert len(catalog.CATALOG) == 40
    from app.lab.strategies import implemented_sids
    assert sorted(d.sid for d in catalog.CATALOG) == implemented_sids()
    for d in catalog.CATALOG:
        validate_params(d.params, {})


def test_features_are_causal():
    df = make_spine(days=3)
    full = features.add_features(df)
    cut = features.add_features(df.iloc[:150].copy())
    for col in ("ema_fast", "rsi", "atr", "vwap", "st_dir", "day_high",
                "prev_close", "gap_pct"):
        a = full[col].iloc[149]
        b = cut[col].iloc[149]
        if pd.isna(a) and pd.isna(b):
            continue
        assert abs(float(a) - float(b)) < 1e-9, \
            "feature %s uses future data (%s vs %s)" % (col, a, b)


def test_entry_fills_next_bar_open():
    # Force a golden cross: flat then a strong jump.
    df = make_spine(days=2, drift=0.0)
    # jump at bar 130 (day 2): craft closes so ema9 crosses ema21 upward
    df.loc[130:, ["open", "high", "low", "close"]] += 400.0
    orig = _patch_spine(df)
    try:
        bt = _bt("F02", df, {"adx_min": 0, "max_trades_per_day": 1,
                             "stop_atr": 5.0, "target_atr": 8.0})
        res = bt.run()
        assert res.status == "done"
        assert res.trades, "no trade despite forced cross"
        t = res.trades[0]
        sig_bar = t["signal_ts"] - 300        # decision = bar open + 300
        assert t["entry_ts"] == sig_bar + 300, \
            "entry must be the NEXT bar (signal bar open %s, entry %s)" % (
                sig_bar, t["entry_ts"])
        fill_bar_open = float(df[df["ts"] == t["entry_ts"]].iloc[0]["open"])
        assert abs(t["legs"][0]["entry"] - (fill_bar_open + 1.0)) < 0.051, \
            "long futures entry must be next open + slippage"
    finally:
        _unpatch(*orig)


def test_stop_checked_before_target():
    df = make_spine(days=2)
    df.loc[130:, ["open", "high", "low", "close"]] += 400.0
    # make every later bar wide enough to hit BOTH stop and target
    df.loc[132:, "high"] = df.loc[132:, "open"] + 200
    df.loc[132:, "low"] = df.loc[132:, "open"] - 200
    orig = _patch_spine(df)
    try:
        bt = _bt("F02", df, {"adx_min": 0, "stop_atr": 0.5,
                             "target_atr": 0.5, "max_trades_per_day": 1})
        res = bt.run()
        assert res.trades
        assert res.trades[0]["exit_reason"] == "stop", \
            "ambiguous intrabar must resolve to STOP, got %s" % \
            res.trades[0]["exit_reason"]
    finally:
        _unpatch(*orig)


def test_no_overnight_fill():
    df = make_spine(days=2)
    # cross on the LAST bar of day 1 -> next bar is next day -> no fill
    df.loc[74, "close"] += 400.0
    df.loc[75:, ["open", "high", "low", "close"]] += 400.0
    orig = _patch_spine(df)
    try:
        bt = _bt("F02", df, {"adx_min": 0, "entry_end_min": 925,
                             "squareoff_min": 929})
        res = bt.run()
        early = [t for t in res.trades
                 if t["entry_ts"] <= bar_ts(0, 74)]
        assert not early, "filled across a day boundary"
    finally:
        _unpatch(*orig)


def test_option_fill_uses_post_decision_snapshot():
    df = make_spine(days=2, base=24000.0)
    # burst upward late morning on day 2 (bars 90+): +50/bar over 3 bars
    for k in range(90, 94):
        df.loc[k:, ["open", "high", "low", "close"]] += 60.0
    decision_ts = bar_ts(1, 18) + 300          # bar 93 close
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "chain.db"
        strikes = [24100.0, 24150.0, 24200.0, 24250.0, 24300.0]
        def rows(mult):
            out = []
            for s in strikes:
                out.append((s, "CE", 100.0 * mult, 99.0 * mult,
                            101.0 * mult, 500000))
                out.append((s, "PE", 80.0 * mult, 79.0 * mult, 81.0 * mult,
                            500000))
            return out
        reader = make_chain_db(p, [
            (decision_ts - 60, rows(1.0)),     # decision snapshot
            (decision_ts + 120, rows(1.10)),   # fill snapshot (+10%)
        ])
        orig = _patch_spine(df)
        try:
            bt = _bt("B01", df, {"look_bars": 3, "burst_atr": 0.4,
                                 "min_oi": 0, "max_spread_pct": 10,
                                 "max_trades_per_day": 1}, reader=reader)
            res = bt.run()
            assert res.trades, "burst should have produced a trade: %s" % \
                res.rejects
            leg = res.trades[0]["legs"][0]
            # ask at fill snap = 101*1.1 = 111.1, +0.25% slip, tick-snapped
            assert leg["entry"] >= 111.1, \
                "fill must use the POST-decision snapshot ask, got %s" % \
                leg["entry"]
        finally:
            _unpatch(*orig)


def test_multileg_rejects_when_one_leg_unquoted():
    df = make_spine(days=2, base=24000.0)
    entry_min_ts = bar_ts(1, 6) + 300           # 09:45 close
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "chain.db"
        # CE quoted, PE row has ltp=0 -> unusable
        reader = make_chain_db(p, [
            (entry_min_ts - 30, [(24000.0, "CE", 100, 99, 101, 500000),
                                 (24000.0, "PE", 0.0, 0, 0, 500000)]),
            (entry_min_ts + 90, [(24000.0, "CE", 100, 99, 101, 500000),
                                 (24000.0, "PE", 0.0, 0, 0, 500000)]),
        ])
        orig = _patch_spine(df)
        try:
            bt = _bt("S01", df, {"entry_min": 585, "min_oi": 0,
                                 "max_spread_pct": 10}, reader=reader)
            res = bt.run()
            assert not res.trades, "order must die whole when a leg has no " \
                                   "usable quote"
            assert any("no_traded_price" in k or "leg_gone" in k or
                       "no_quote" in k for k in res.rejects), res.rejects
        finally:
            _unpatch(*orig)


def test_unlisted_strike_rejected():
    df = make_spine(days=2, base=24000.0)
    ts0 = bar_ts(1, 6) + 300
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "chain.db"
        # only ATM listed; S02 wants 3 steps OTM -> not listed
        reader = make_chain_db(p, [
            (ts0 - 30, [(24000.0, "CE", 100, 99, 101, 500000),
                        (24000.0, "PE", 90, 89, 91, 500000)]),
            (ts0 + 90, [(24000.0, "CE", 100, 99, 101, 500000),
                        (24000.0, "PE", 90, 89, 91, 500000)]),
        ])
        orig = _patch_spine(df)
        try:
            bt = _bt("S02", df, {"entry_min": 585, "strike_offset": 3,
                                 "min_oi": 0, "max_spread_pct": 10},
                     reader=reader)
            res = bt.run()
            assert not res.trades
            assert "strike_not_listed" in res.rejects, res.rejects
        finally:
            _unpatch(*orig)


def test_costs_agree_with_audited_charges_module():
    """One long-option round trip must cost the same rupees under
    app/paper/charges.py (audited, test-pinned) and app/lab/costs.py."""
    from app.paper.charges import charges as paper_charges
    entry, exit_px, qty = 100.0, 120.0, 75
    theirs = paper_charges(entry, exit_px, qty)
    mine = costs_mod.order_cost("OPT", "BUY", entry, qty, "2026-09-14") + \
        costs_mod.order_cost("OPT", "SELL", exit_px, qty, "2026-09-14")
    assert abs(theirs - mine) < 0.02, "cost models drifted: %s vs %s" % (
        theirs, mine)


def test_lot_size_eras():
    # 65 verified against the live NFO master on 24-Sep-2026 (and matches
    # config.closing_lot_size). Extend LOT_SIZE_ERAS only from a verified
    # circular — this test then pins the boundary.
    assert lot_size_for("2026-09-14") == 65
    from app.config import get_settings
    assert lot_size_for("2026-09-24") == get_settings().closing_lot_size


def test_squareoff_forced_exit():
    df = make_spine(days=2)
    # cross at day-2 bar 15 (10:30), well before the 11:40 squareoff below
    df.loc[90:, ["open", "high", "low", "close"]] += 400.0
    orig = _patch_spine(df)
    try:
        bt = _bt("F02", df, {"adx_min": 0, "stop_atr": 5.0,
                             "target_atr": 8.0, "squareoff_min": 700,
                             "max_trades_per_day": 1})
        res = bt.run()
        assert res.trades
        t = res.trades[0]
        assert t["exit_reason"] in ("squareoff", "reverse_cross"), t
        exit_min = (t["exit_ts"] + 19800) % 86400 // 60
        assert exit_min <= 706, "exit after the squareoff minute"
    finally:
        _unpatch(*orig)


def test_store_immutability():
    from app.lab.store import LabStore
    with tempfile.TemporaryDirectory() as td:
        st = LabStore(Path(td) / "lab.db")
        st.ensure_version1("F01", {"a": 1})
        st.ensure_version1("F01", {"a": 999})   # second call must not clobber
        assert st.get_params("F01", 1) == {"a": 1}
        r1 = st.create_run("F01", 1, "insample", "5m", 0, 1, 100.0, {}, "")
        r2 = st.create_run("F01", 1, "insample", "5m", 0, 1, 100.0, {}, "")
        assert r2 == r1 + 1, "re-running must create a NEW run row"
        st.save_paper_day("2026-09-14", "F01", 1, 2, 100.0, {"x": 1})
        st.save_paper_day("2026-09-14", "F01", 1, 9, -999.0, {"x": 2})
        days = st.paper_days("F01")
        assert days[0]["pnl_net"] == 100.0, "day ledger must be immutable"


def test_risk_engine_fails_closed_and_blocks():
    from app.lab import risk
    from app.lab.store import LabStore
    with tempfile.TemporaryDirectory() as td:
        st = LabStore(Path(td) / "lab.db")
        d = risk.check_order("F01", "2026-09-14", [], 0.0, False, 1, store=st)
        assert d.allowed
        risk.trip_emergency("test", flatten=False, store=st)
        d = risk.check_order("F01", "2026-09-14", [], 0.0, False, 1, store=st)
        assert not d.allowed and d.code == "emergency_stop"
        risk.clear_emergency(store=st)

        class _Broken:
            def risk_config(self):
                raise RuntimeError("boom")
        d = risk.check_order("F01", "2026-09-14", [], 0.0, False, 1,
                             store=_Broken())
        assert not d.allowed and d.code == "risk_engine_error", \
            "a broken risk engine must not allow orders"


def test_insufficient_data_is_disclosed():
    df = make_spine(days=1).iloc[:10]
    orig = _patch_spine(df)
    try:
        bt = _bt("F02", df)
        res = bt.run()
        assert res.status == "insufficient_data"
    finally:
        _unpatch(*orig)




def test_supertrend_direction_is_mapped():
    """st_dir must actually carry the indicator's up/down — a wrong label
    map reads as 'flat forever' and silently disables every supertrend
    strategy (found 26-Sep: 60 days of real data produced zero flips)."""
    df = make_spine(days=3)
    # alternate regimes so the band flips at least once
    df.loc[80:120, ["open", "high", "low", "close"]] += 300.0
    df.loc[121:, ["open", "high", "low", "close"]] -= 250.0
    f = features.add_features(df)
    assert (f["st_dir"] != 0).any(), "st_dir never leaves 0 — label map broken"
    flips = ((f["st_dir"].shift(1) <= 0) & (f["st_dir"] > 0)) | \
            ((f["st_dir"].shift(1) >= 0) & (f["st_dir"] < 0))
    assert flips.any(), "no supertrend flips despite forced regime changes"


def test_delta_for_accepts_iso_expiry():
    """Chain-archive expiries are ISO strings; the delta path must parse
    them (found 26-Sep: B05/V05 backtests crashed on str.year)."""
    from datetime import datetime as _dt
    from app.lab.fills import Quote, _delta_for
    q = Quote(token=1, tradingsymbol="x", expiry="2026-09-29",
              strike=24000.0, right="CE", ltp=100.0, bid=99.0, ask=101.0,
              oi=1000, ts=0, iv=15.0)
    now = int(_dt(2026, 9, 24, 12, 0).timestamp())
    d = _delta_for(q, 24000.0, now)
    assert d is not None and 0.3 < d < 0.7, d


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print("%d tests passed" % len(fns))
