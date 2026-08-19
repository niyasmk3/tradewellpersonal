"""The modelled-premium layer: vol curve, carry, and what it refuses to price.

The Closing Day study's option numbers are only as good as these pieces, and
two of them are places where a silent wrong answer looks perfectly reasonable:
extrapolating the vol curve past its evidence, and pricing a contract that has
already settled.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing.calendar import IST
from app.closing.pricing import IvCurve, OptionModel, atm_strike, dte_days

CURVE = [{"dte_mid": 0.1, "ratio": 1.5},
         {"dte_mid": 1.0, "ratio": 1.0},
         {"dte_mid": 7.0, "ratio": 0.85}]


def _ts(y, m, d, hh, mm) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=IST).timestamp()


def test_atm_strike_rounds_to_the_50_point_grid():
    assert atm_strike(24337.7) == 24350.0
    assert atm_strike(24324.9) == 24300.0
    # An exact midpoint is a genuine tie — both strikes are equally ATM.
    # Python's banker's rounding breaks it by parity, which alternates and so
    # cannot drift the study toward one side; always-round-up would have
    # nudged every tie into a higher strike and quietly favoured puts.
    assert atm_strike(24325.0) == 24300.0
    assert atm_strike(24375.0) == 24400.0


def test_iv_curve_interpolates_between_measured_points():
    c = IvCurve(CURVE)
    assert c.ratio(0.1) == 1.5
    assert c.ratio(1.0) == 1.0
    mid = c.ratio(0.55)
    assert 1.0 < mid < 1.5


def test_iv_curve_extrapolates_flat_not_linear():
    """Past the last observation the curve has no evidence, so it must hold
    the last measured level rather than continue its slope into fiction."""
    c = IvCurve(CURVE)
    assert c.ratio(30.0) == 0.85
    assert c.ratio(0.0) == 1.5
    assert c.ratio(-5.0) == 1.5


def test_iv_curve_falls_back_to_raw_vix_and_says_so():
    c = IvCurve(None)
    assert c.is_fallback is True
    assert c.ratio(3.0) == 1.0
    assert abs(c.sigma(12.0, 3.0) - 0.12) < 1e-9


def test_premium_is_none_after_settlement():
    """An expired leg has no mid. Returning intrinsic here would turn a
    modelling gap into a fabricated fill that the backtest would happily book."""
    m = OptionModel(IvCurve(CURVE), 0.1175)
    exp = date(2026, 8, 18)
    after = _ts(2026, 8, 18, 15, 45)
    assert m.premium(24200, 24200, after, exp, False, 11.4) is None
    before = _ts(2026, 8, 18, 9, 50)
    assert m.premium(24200, 24200, before, exp, False, 11.4) > 0


def test_carry_lifts_the_call_and_cheapens_the_put():
    """The whole point of fitting carry: at index spot the ATM put and call
    price nearly equal, but the real forward sits above spot, which is a
    directional bias in a directional study."""
    exp = date(2026, 8, 25)
    ts = _ts(2026, 8, 24, 15, 0)
    lo = OptionModel(IvCurve(CURVE), 0.065)
    hi = OptionModel(IvCurve(CURVE), 0.1175)
    ce_lo = lo.premium(24200, 24200, ts, exp, True, 11.4)
    ce_hi = hi.premium(24200, 24200, ts, exp, True, 11.4)
    pe_lo = lo.premium(24200, 24200, ts, exp, False, 11.4)
    pe_hi = hi.premium(24200, 24200, ts, exp, False, 11.4)
    assert ce_hi > ce_lo
    assert pe_hi < pe_lo


def test_dte_days_measures_to_the_1530_settlement():
    exp = date(2026, 8, 25)
    assert abs(dte_days(_ts(2026, 8, 25, 15, 30), exp)) < 1e-6
    assert abs(dte_days(_ts(2026, 8, 24, 15, 30), exp) - 1.0) < 1e-6
    assert dte_days(_ts(2026, 8, 25, 9, 50), exp) > 0
