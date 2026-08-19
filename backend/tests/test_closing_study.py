"""The backtest's own mechanics, on synthetic bars with a known answer.

These pin the parts a reader has to trust before any headline means anything:
which reference decides the side, that the fill is not the same instant as the
signal, that the deadband and the expiry roll behave, and that the reported
confidence interval does not wander between identical runs.

Sessions are built with the day's OPEN and its 15:00 print set independently,
because the two signal modes read different references and a fixture that
collapses them can only test one.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing.calendar import IST
from app.closing.pricing import IvCurve, OptionModel
from app.closing.study import (
    DEFAULT_SIGNAL_MODE,
    SIGNAL_DAY_OPEN,
    SIGNAL_PREV_CLOSE,
    StudyConfig,
    VixLookup,
    _bootstrap_ci,
    build_days,
    build_trades,
)

BARS = [(9, 15), (9, 50), (15, 0), (15, 10), (15, 25)]


def _session(d: date, open_at: float, at_1500: float, fill: float = None) -> list:
    """One synthetic session.

    09:15 opens at `open_at`; the 15:00 bar OPENS at `at_1500` (the print the
    signal is read from) and CLOSES at `fill` (the price the trade is filled
    at, default `at_1500`); the tail sits at `fill` so close_ref has a clean
    session end.
    """
    fill = at_1500 if fill is None else fill
    level_at = {(9, 15): open_at, (9, 50): open_at,
                (15, 0): at_1500, (15, 10): fill, (15, 25): fill}
    rows = []
    for hm in BARS:
        ts = int(datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=IST).timestamp())
        o = level_at[hm]
        c = fill if hm == (15, 0) else o
        rows.append((ts, o, max(o, c), min(o, c), c))
    return rows


def _frame(sessions: dict) -> pd.DataFrame:
    rows = []
    for d, spec in sorted(sessions.items()):
        rows.extend(_session(d, *spec))
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])


def _vix(spine: pd.DataFrame, level: float = 12.0) -> VixLookup:
    return VixLookup(pd.DataFrame({"ts": spine["ts"], "open": level, "high": level,
                                   "low": level, "close": level}))


MODEL = OptionModel(IvCurve([{"dte_mid": 0.1, "ratio": 1.5},
                             {"dte_mid": 1.0, "ratio": 1.0},
                             {"dte_mid": 7.0, "ratio": 0.85}]), 0.1175)

# Mon-Thu of a post-switch week, so Tuesday is the weekly expiry.
MON, TUE, WED, THU = (date(2026, 8, 17), date(2026, 8, 18),
                      date(2026, 8, 19), date(2026, 8, 20))


def _run(sessions: dict, cfg: StudyConfig = None):
    spine = _frame(sessions)
    days = build_days(spine)
    return build_trades(days, _vix(spine), MODEL, cfg or StudyConfig())


def _by_date(trades):
    return {t["date"]: t for t in trades}


# --- which reference decides the side --------------------------------------

def test_the_default_reference_is_todays_open():
    """Pinned deliberately. The previous-close reference is barely separable
    from an always-buy-CE control, because it inherits NIFTY's upward drift;
    day_open splits CE/PE evenly and so earns from direction. Flipping this
    default silently would change every headline the module publishes."""
    assert DEFAULT_SIGNAL_MODE == SIGNAL_DAY_OPEN
    assert StudyConfig().signal_mode == SIGNAL_DAY_OPEN


def test_day_open_buys_a_call_on_a_green_session_and_a_put_on_a_red_one():
    # Tue opens 24400 and sits at 24300 by 15:00 -> red -> PE.
    # Wed opens 24300 and sits at 24500 by 15:00 -> green -> CE.
    trades, _ = _run({MON: (24400.0, 24400.0), TUE: (24400.0, 24300.0),
                      WED: (24300.0, 24500.0), THU: (24500.0, 24500.0)})
    by_date = _by_date(trades)
    assert by_date[TUE.isoformat()]["direction"] == "PE"
    assert by_date[WED.isoformat()]["direction"] == "CE"


def test_day_open_ignores_yesterdays_close_entirely():
    """A session that is red on the day but still above yesterday's close is
    the case that separates the two rules — the default must read the body."""
    trades, _ = _run({MON: (24000.0, 24000.0),      # closes at 24000
                      TUE: (24500.0, 24300.0),      # red day, but > 24000
                      WED: (24300.0, 24300.0), THU: (24300.0, 24300.0)})
    t = _by_date(trades)[TUE.isoformat()]
    assert t["reference"] == 24500.0        # today's open
    assert t["prev_close"] == 24000.0       # yesterday's close, recorded but unused
    assert t["direction"] == "PE"


def test_prev_close_mode_still_reads_yesterdays_close():
    """The original hypothesis stays implemented — the tab reports both."""
    cfg = StudyConfig(signal_mode=SIGNAL_PREV_CLOSE)
    trades, _ = _run({MON: (24000.0, 24000.0),
                      TUE: (24500.0, 24300.0),      # red day, but above 24000
                      WED: (24300.0, 24300.0), THU: (24300.0, 24300.0)}, cfg)
    t = _by_date(trades)[TUE.isoformat()]
    assert t["reference"] == 24000.0
    assert t["direction"] == "CE"           # opposite call to day_open, by design


def test_an_unknown_signal_mode_raises_rather_than_guessing():
    cfg = StudyConfig(signal_mode="vibes")
    with pytest.raises(ValueError, match="unknown signal_mode"):
        _run({MON: (24400.0, 24300.0), TUE: (24400.0, 24300.0),
              WED: (24300.0, 24500.0), THU: (24500.0, 24500.0)}, cfg)


# --- execution mechanics ----------------------------------------------------

def test_signal_reads_the_1500_open_but_the_fill_is_that_bar_s_close():
    """Reading and filling at the same instant would be a physically
    impossible trade. The study pays for the five minutes it takes to act."""
    trades, _ = _run({MON: (24400.0, 24400.0),
                      TUE: (24400.0, 24300.0, 24360.0),   # print 24300, fill 24360
                      WED: (24360.0, 24360.0), THU: (24360.0, 24360.0)})
    t = _by_date(trades)[TUE.isoformat()]
    assert t["signal_price"] == 24300.0     # the 15:00 print decided it
    assert t["entry_spot"] == 24360.0       # the fill came later, higher
    assert t["direction"] == "PE"           # and the signal is NOT re-read
    assert t["strike"] == 24350.0           # ATM is struck at the FILL


def test_signed_move_is_positive_only_when_the_index_went_the_signal_s_way():
    trades, _ = _run({MON: (24400.0, 24400.0), TUE: (24400.0, 24300.0),
                      WED: (24200.0, 24200.0), THU: (24200.0, 24200.0)})
    t = _by_date(trades)[TUE.isoformat()]
    assert t["direction"] == "PE"
    assert t["spot_move_pts"] < 0           # index fell
    assert t["signed_move_pts"] > 0         # which is the way the PE pointed


def test_deadband_skips_a_quiet_session():
    cfg = StudyConfig(deadband_pts=50.0)
    trades, skipped = _run({MON: (24400.0, 24400.0),
                            TUE: (24400.0, 24390.0),    # only 10 points of body
                            WED: (24390.0, 24500.0), THU: (24500.0, 24500.0)}, cfg)
    assert TUE.isoformat() not in {t["date"] for t in trades}
    assert skipped.get("inside deadband") == 1


def test_a_perfectly_flat_session_is_no_signal_at_all():
    """15:00 exactly at the open points nowhere; with the default zero deadband
    it must be skipped rather than resolved to an arbitrary side."""
    trades, skipped = _run({MON: (24400.0, 24400.0), TUE: (24400.0, 24400.0),
                            WED: (24400.0, 24500.0), THU: (24500.0, 24500.0)})
    assert TUE.isoformat() not in {t["date"] for t in trades}
    assert skipped.get("inside deadband") == 1


def test_a_trade_opened_on_expiry_day_rolls_to_the_next_week():
    trades, _ = _run({MON: (24400.0, 24400.0), TUE: (24400.0, 24300.0),
                      WED: (24300.0, 24500.0), THU: (24500.0, 24500.0)})
    by_date = _by_date(trades)
    # Tuesday IS the weekly expiry, so its trade must be in the following one.
    assert by_date[TUE.isoformat()]["expiry"] == "2026-08-25"
    assert by_date[TUE.isoformat()]["dte_entry"] > 6
    # Wednesday holds the next Tuesday, six days out.
    assert by_date[WED.isoformat()]["expiry"] == "2026-08-25"


def test_charges_and_spread_make_net_strictly_worse_than_gross():
    trades, _ = _run({MON: (24400.0, 24400.0), TUE: (24400.0, 24300.0),
                      WED: (24200.0, 24200.0), THU: (24200.0, 24200.0)})
    t = _by_date(trades)[TUE.isoformat()]
    assert t["fill_in"] > t["mid_in"]        # bought through the offer
    assert t["fill_out"] < t["mid_out"]      # sold into the bid
    assert t["charges_rs"] > 0
    assert t["net_rs"] < t["gross_rs"]


def test_bootstrap_ci_is_deterministic_and_declines_tiny_samples():
    xs = [float(i % 17) - 8 for i in range(120)]
    assert _bootstrap_ci(xs, 500) == _bootstrap_ci(xs, 500)
    lo, hi = _bootstrap_ci(xs, 500)
    assert lo < hi
    assert _bootstrap_ci([1.0, 2.0, 3.0], 500) is None
