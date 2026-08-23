"""The 15:00 research logger: frozen divergence semantics, skew leg math,
window stamping, and the first-full-row-per-date audit contract.

The two definitions are PRE-REGISTERED (2026-08-22) — these tests pin them so
an accidental edit fails loudly instead of silently resetting a live count."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import snapshot
from app.closing.calendar import IST
from app.options.iv import bs_price

TODAY = date(2026, 8, 20)          # Thursday — a trading day
NOW = datetime(2026, 8, 20, 15, 6, tzinfo=IST)


def _stamps(g14=24300.0, g15=24340.0):
    out = {"date": TODAY.isoformat()}
    if g14 is not None:
        out["g1400"] = {"price": g14, "at": "2026-08-20T14:01:00+05:30"}
    if g15 is not None:
        out["g1500"] = {"price": g15, "at": "2026-08-20T15:00:30+05:30"}
    return out


def _nifty(day_open=24200.0, p1400=24280.0, p1500=24310.0):
    return {"day_open": day_open, "p1400": p1400, "p1500": p1500}


# --- divergence: the frozen definition --------------------------------------

def test_divergence_math_and_ce_alignment():
    # GIFT +40 vs NIFTY +30 -> divergence +10, aligned +10 for CE, no flag.
    row = snapshot.build_row(TODAY, NOW, _stamps(), _nifty(), {})
    assert row["direction"] == "CE"
    assert row["divergence"]["pts"] == 10.0
    assert row["divergence"]["aligned_pts"] == 10.0
    assert row["divergence"]["flag"] is False


def test_divergence_flags_when_offshore_fights_the_trade():
    # GIFT -20 while NIFTY ran +30 -> divergence -50; CE night -> flag.
    row = snapshot.build_row(TODAY, NOW, _stamps(g15=24280.0), _nifty(), {})
    assert row["divergence"]["pts"] == -50.0
    assert row["divergence"]["flag"] is True


def test_divergence_alignment_negates_for_pe():
    # Red day (PE). NIFTY's last hour ran -30 but GIFT only -5: divergence
    # +25 (offshore refusing the fall) -> aligned -25 against the PE -> flag.
    # The sign convention is the frozen spec.
    row = snapshot.build_row(TODAY, NOW, _stamps(g14=24300.0, g15=24295.0),
                             _nifty(day_open=24400.0, p1400=24360.0, p1500=24330.0), {})
    assert row["direction"] == "PE"
    assert row["divergence"]["pts"] == 25.0
    assert row["divergence"]["aligned_pts"] == -25.0
    assert row["divergence"]["flag"] is True


def test_missing_gift_stamp_leaves_divergence_unknown():
    # Unknown never fires and never clears: no 14:00 stamp -> everything None.
    row = snapshot.build_row(TODAY, NOW, _stamps(g14=None), _nifty(), {})
    assert row["divergence"]["pts"] is None
    assert row["divergence"]["flag"] is None


def test_flat_body_means_no_direction_and_no_flag():
    row = snapshot.build_row(TODAY, NOW, _stamps(),
                             _nifty(day_open=24310.0, p1500=24310.0), {})
    assert row["direction"] is None
    assert row["divergence"]["pts"] is not None   # the fact still logs
    assert row["divergence"]["flag"] is None      # but no direction, no flag


def test_registered_definitions_are_pinned():
    assert snapshot.REGISTERED_ON == "2026-08-22"
    assert snapshot.DIVERGENCE_FLAG_PTS == -10.0
    assert "IV_30d_PE - IV_30d_CE" in snapshot.SKEW_DEF


# --- skew leg math -----------------------------------------------------------

def _quote(bid, ask, oi=1000):
    return {"depth": {"buy": [{"price": bid, "quantity": 75, "orders": 1}],
                      "sell": [{"price": ask, "quantity": 75, "orders": 1}]},
            "last_price": (bid + ask) / 2.0, "oi": oi, "volume": 5000}


def test_leg_recovers_iv_from_bs_mid():
    # Price an ATM call at 14% vol, feed it back as bid/ask -> IV ~14.
    spot, strike, t = 24300.0, 24300.0, 5.0 / 365.0
    mid = bs_price(spot, strike, t, 0.14, True)
    leg = snapshot._leg(_quote(mid - 0.5, mid + 0.5), strike, True, spot, t)
    assert leg["iv"] is not None
    assert abs(leg["iv"] - 14.0) < 0.3
    assert 0.4 < leg["delta"] < 0.65          # ATM call sits near 0.5
    assert leg["spread"] == 1.0
    assert leg["oi"] == 1000


def test_leg_with_empty_book_falls_back_to_last_price():
    q = {"depth": {"buy": [], "sell": []}, "last_price": 100.0, "oi": 5}
    leg = snapshot._leg(q, 24300.0, True, 24300.0, 5.0 / 365.0)
    assert leg["mid"] == 100.0
    assert leg["bid"] is None and leg["spread"] is None


def test_bs_delta_put_is_negative():
    d = snapshot._bs_delta(24300.0, 24300.0, 5.0 / 365.0, 14.0, False)
    assert d is not None and -0.65 < d < -0.35


# --- stamps and the append-only log ------------------------------------------

def test_stamp_gift_only_inside_windows(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(snapshot, "fetch_gift_quote",
                        lambda timeout=10.0: calls.append(1) or
                        {"price": 24321.0, "at": "x", "expiry": "e"})
    p = tmp_path / "stamps.json"
    at = lambda h, m: datetime(2026, 8, 20, h, m, tzinfo=IST)
    assert snapshot.stamp_gift(at(13, 59), p) is None      # before the window
    assert snapshot.stamp_gift(at(14, 1), p) == "g1400"
    assert snapshot.stamp_gift(at(14, 3), p) is None       # first reading wins
    assert snapshot.stamp_gift(at(14, 30), p) is None      # between windows
    assert snapshot.stamp_gift(at(15, 0), p) == "g1500"
    assert len(calls) == 2
    saved = json.loads(p.read_text())
    assert saved["g1400"]["price"] == 24321.0


def test_run_snapshot_is_first_row_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "fetch_gift_quote",
                        lambda timeout=10.0: {"price": 24340.0, "at": "x",
                                              "expiry": "25-Aug-2026"})
    log_p, stamp_p = tmp_path / "snap.jsonl", tmp_path / "stamps.json"
    r1 = snapshot.run_snapshot(None, NOW, log_p, stamp_p)
    assert r1["logged"] is True
    assert r1["row"]["gift"]["g1500"]["price"] == 24340.0
    # kite=None degrades honestly: chain is an error note, divergence unknown.
    assert "error" in r1["row"]["skew"]
    assert r1["row"]["divergence"]["flag"] is None
    r2 = snapshot.run_snapshot(None, NOW, log_p, stamp_p)
    assert r2["logged"] is False and r2["reason"] == "already logged"
    assert len(log_p.read_text().splitlines()) == 1


def test_run_snapshot_refuses_non_trading_days(tmp_path):
    sat = datetime(2026, 8, 22, 15, 6, tzinfo=IST)
    r = snapshot.run_snapshot(None, sat, tmp_path / "s.jsonl", tmp_path / "st.json")
    assert r["logged"] is False and "not a trading day" in r["reason"]


def test_snapshots_newest_first(tmp_path):
    p = tmp_path / "snap.jsonl"
    with p.open("w") as fh:
        fh.write(json.dumps({"date": "2026-08-19", "n": 1}) + "\n")
        fh.write(json.dumps({"date": "2026-08-20", "n": 2}) + "\n")
        fh.write(json.dumps({"date": "2026-08-19", "n": 99}) + "\n")   # dupe loses
    rows = snapshot.snapshots(30, p)
    assert [r["date"] for r in rows] == ["2026-08-20", "2026-08-19"]
    assert rows[1]["n"] == 1
