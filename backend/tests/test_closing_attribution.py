"""The loss-attribution block and its pre-registered calendar flags.

The live scoreboard's count only means something if the flag definitions
cannot drift, so the registration date, the bridge/month-end semantics, and
the unknown-never-passes rule are all pinned. The decomposition's one
structural promise — components sum to net % — is pinned too.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import attribution
from app.closing.study import EXIT_BAR, SIGNAL_BAR, Day


def test_registration_is_frozen():
    assert attribution.REGISTERED_ON == date(2026, 8, 19)
    assert attribution.MIN_LIVE_SAMPLE == 30
    assert [f["key"] for f in attribution.FLAG_DEFINITIONS] == [
        "holiday_bridge", "month_end"]


# --- calendar flags ---------------------------------------------------------

# A synthetic calendar crossing a month boundary: Fri 28-Aug, Mon 31-Aug
# (month-end), Tue 01-Sep, Wed 02-Sep, Fri 04-Sep (Thu was a holiday).
SESSIONS = [date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 31),
            date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 4)]


def _trade(d: str, exit_d: str, **kw):
    t = {"date": d, "exit_date": exit_d, "net_pct": 0.0, "net_rs": 0.0,
         "dte_bucket": "2-3", "gap_bucket": "50-100", "vix_in": 12.0,
         "signed_move_pts": 1.0}
    t.update(kw)
    return t


def test_bridge_flag_semantics():
    """Carry 2 and 4+ are bridges; a plain Fri->Mon weekend is NOT — the
    weekend bucket flipped sign between windows and stays unflagged."""
    trades = [
        _trade("2026-08-27", "2026-08-28"),   # carry 1: normal night
        _trade("2026-09-02", "2026-09-04"),   # carry 2: mid-week holiday
        _trade("2026-08-28", "2026-08-31"),   # carry 3: plain weekend
        _trade("2026-08-27", "2026-08-31"),   # carry 4: long bridge
    ]
    attribution.annotate(trades, SESSIONS)
    assert [t["f_holiday_bridge"] for t in trades] == [False, True, False, True]
    assert "weekend carry" in trades[2]["tags"]
    assert "holiday bridge" not in trades[2]["tags"]


def test_month_end_flag_and_unknowns_never_clear():
    trades = [
        _trade("2026-08-31", "2026-09-01"),   # last session of August
        _trade("2026-09-01", "2026-09-02"),   # first session of September
        _trade("2026-09-04", "2026-09-07"),   # calendar ends here: unknown
    ]
    attribution.annotate(trades, SESSIONS)
    assert trades[0]["f_month_end"] is True
    assert trades[0]["f_calendar_clear"] is False
    assert "month-start entry" in trades[1]["tags"]
    assert trades[1]["f_calendar_clear"] is True
    # No next session on record: month-end is unknowable, and an unknown
    # night is UNKNOWN — it must never count as clear.
    assert trades[2]["f_month_end"] is None
    assert trades[2]["f_calendar_clear"] is None


# --- decomposition ----------------------------------------------------------

SIG_TS, EXIT_TS = 1_000_000, 1_067_500


class StubModel:
    """Deterministic premiums keyed on the repricing step's timestamp."""

    def premium(self, spot, strike, ts, expiry, is_call, vix):
        return {SIG_TS: 80.0, EXIT_TS: 50.0}[ts]


def _days():
    d1, d2 = Day(date(2026, 8, 27)), Day(date(2026, 8, 28))
    d1.bars[SIGNAL_BAR] = (100.0, 101.0, 99.0, 100.0, SIG_TS)
    d2.bars[EXIT_BAR] = (100.0, 101.0, 99.0, 100.0, EXIT_TS)
    return {d1.d: d1, d2.d: d2}


def test_decompose_components_sum_to_net_and_reason_is_dominant():
    t = _trade("2026-08-27", "2026-08-28", expiry="2026-09-01", direction="CE",
               mid_in=100.0, mid_out=55.0, fill_in=100.0, exit_spot=100.0,
               strike=100.0, vix_in=12.0, net_pct=-50.0)
    attribution.decompose([t], _days(), StubModel())
    # 100 -> 80 (direction -20) -> 50 (theta -30) -> 55 (vega +5); costs are
    # the remainder that reconciles to the trade's actual net.
    assert (t["direction_pct"], t["theta_pct"], t["vega_pct"]) == (-20.0, -30.0, 5.0)
    total = t["direction_pct"] + t["theta_pct"] + t["vega_pct"] + t["costs_pct"]
    assert abs(total - t["net_pct"]) < 0.05
    assert t["loss_reason"] == "theta_decay"


def test_decompose_winner_carries_no_loss_reason():
    t = _trade("2026-08-27", "2026-08-28", expiry="2026-09-01", direction="CE",
               mid_in=40.0, mid_out=55.0, fill_in=40.0, exit_spot=100.0,
               strike=100.0, vix_in=12.0, net_pct=30.0)
    attribution.decompose([t], _days(), StubModel())
    assert t["loss_reason"] is None


# --- ladder + live scoreboard -----------------------------------------------

def test_ladder_kept_requires_strictly_false():
    trades = [
        _trade("2026-08-27", "2026-08-28"),   # clear
        _trade("2026-09-02", "2026-09-04"),   # bridge
        _trade("2026-08-31", "2026-09-01"),   # month-end
        _trade("2026-09-04", "2026-09-07"),   # month-end UNKNOWN
    ]
    attribution.annotate(trades, SESSIONS)
    rungs = attribution.ladder(trades)
    assert [r["kept"]["n"] for r in rungs] == [4, 3, 1]
    # The unknown month-end fell out at the rung that needed the missing
    # input and is counted there, not silently kept.
    assert rungs[2]["unknown_n"] == 1


def test_live_scoreboard_counts_only_nights_after_registration():
    trades = [
        _trade("2026-08-18", "2026-08-19"),   # before registration
        _trade("2026-08-19", "2026-08-20"),   # ON the registration date:
                                              # "strictly after" excludes it
        _trade("2026-08-27", "2026-08-28"),   # live, clear
        _trade("2026-09-02", "2026-09-04"),   # live, flagged (bridge)
        _trade("2026-08-31", "2026-09-01"),   # live, flagged (month-end)
    ]
    attribution.annotate(trades, SESSIONS)
    live = attribution.live_scoreboard(trades)
    assert live["live_nights"] == 3
    assert live["flagged_nights"] == 2
    assert live["verdict_due"] == attribution.MIN_LIVE_SAMPLE - 2


def test_bucket_verdicts():
    assert attribution._verdict(-1.0, -2.0) == "consistent-bad"
    assert attribution._verdict(-1.0, 2.0) == "flips — noise"
    assert attribution._verdict(1.0, 2.0) == "consistent-positive"
    assert attribution._verdict(None, -2.0) == "too few trades"
