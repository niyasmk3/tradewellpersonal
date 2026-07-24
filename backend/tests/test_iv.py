"""Implied-volatility solver tests (run standalone or under pytest).

The IV column is a regime gauge the trader will glance at mid-session, so the
tests pin the properties that make it trustworthy: the solver inverts its own
pricer exactly, refuses to invent a number for impossible premiums, and the
chain builder only fills it near the money.

Run:  python backend/tests/test_iv.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import math
import time
from datetime import date, datetime, timedelta, timezone

from app.options.iv import RISK_FREE, bs_price, implied_vol, years_to_expiry

_IST = timezone(timedelta(hours=5, minutes=30))


def test_put_call_parity():
    # C - P = S - K·e^(-rT), the identity any BS implementation must satisfy.
    s, k, t, sig = 24000.0, 24100.0, 5 / 365, 0.14
    c = bs_price(s, k, t, sig, True)
    p = bs_price(s, k, t, sig, False)
    lhs = c - p
    rhs = s - k * math.exp(-RISK_FREE * t)
    assert abs(lhs - rhs) < 1e-6, f"parity broken: {lhs} vs {rhs}"
    print(f"  PARITY -> C-P {lhs:.4f} == S-Ke^-rT {rhs:.4f}")


def test_iv_round_trip():
    # Price at a known vol, recover the vol. ATM, OTM and ITM, calls and puts.
    t = 6 / 365
    for strike in (23800.0, 24000.0, 24200.0):
        for is_call in (True, False):
            for sigma in (0.10, 0.14, 0.25):
                px = bs_price(24000.0, strike, t, sigma, is_call)
                iv = implied_vol(px, 24000.0, strike, t, is_call)
                assert iv is not None, f"no IV for K={strike} call={is_call} sig={sigma}"
                assert abs(iv - sigma * 100) < 0.11, f"K={strike}: {iv} vs {sigma * 100}"
    print("  ROUND  -> 18 configurations recover their input vol within 0.1 pt")


def test_iv_refuses_impossible_premiums():
    t = 6 / 365
    # At/below intrinsic (stale or arbitrage print): no time value, no IV.
    assert implied_vol(200.0, 24200.0, 24000.0, t, True) is None
    # Above the 500%-vol price: bad tick.
    assert implied_vol(20000.0, 24000.0, 24000.0, t, True) is None
    # Degenerate inputs.
    assert implied_vol(None, 24000.0, 24000.0, t, True) is None
    assert implied_vol(100.0, None, 24000.0, t, True) is None
    assert implied_vol(100.0, 24000.0, 24000.0, 0.0, True) is None
    assert implied_vol(-5.0, 24000.0, 24000.0, t, True) is None
    print("  REFUSE -> below-intrinsic, absurd and degenerate premiums all -> None")


def test_years_to_expiry():
    expiry = date(2026, 7, 30)
    # From 10:00 IST on the 28th to 15:30 IST on the 30th: 2 days 5.5 hours.
    now = datetime(2026, 7, 28, 10, 0, tzinfo=_IST).timestamp()
    got = years_to_expiry(expiry, now)
    want = (2 * 86400 + 5.5 * 3600) / (365 * 86400)
    assert abs(got - want) < 1e-9, f"{got} vs {want}"
    # Past expiry and missing expiry are both 0, never negative.
    assert years_to_expiry(expiry, now + 90 * 86400) == 0.0
    assert years_to_expiry(None) == 0.0
    print("  TIME   -> settlement anchored at 15:30 IST; past/missing -> 0")


def test_chain_fills_iv_near_atm_only():
    # The builder solves ±5 strikes around ATM and leaves the wings None.
    from app.kite.instruments import OptionUniverse, StrikePair
    from app.options.chain import OptionChainBuilder
    from app.state import MarketState

    state = MarketState()

    class _Meta:
        symbol, spot_token, fut_token, lot_size = "NIFTY", 1, 2, 65
    state.underlyings["NIFTY"] = _Meta()

    strikes = {}
    token = 100
    spot = 24000.0
    # Price the synthetic quotes with the SAME clock the chain builder uses —
    # a hardcoded 5/365 here drifts against the 15:30-IST settlement anchor as
    # wall-clock time moves, and the recovered IV walks away from 15.0 by up
    # to a vol point depending on when the suite runs (it failed at 1.0 exactly
    # on a Saturday-noon run after passing Friday evening).
    expiry = (datetime.fromtimestamp(time.time(), tz=_IST) + timedelta(days=5)).date()
    t_years = years_to_expiry(expiry)
    for k in range(23400, 24650, 50):
        ce_t, pe_t = token, token + 1
        token += 2
        strikes[float(k)] = StrikePair(strike=float(k), ce_token=ce_t, pe_token=pe_t)
        # Quote every leg AT a known vol so in-band rows must solve back to it.
        state.ticks[ce_t] = {"last_price": round(bs_price(spot, float(k), t_years, 0.15, True), 2)}
        state.ticks[pe_t] = {"last_price": round(bs_price(spot, float(k), t_years, 0.15, False), 2)}
    state.ticks[1] = {"last_price": spot}

    uni = OptionUniverse(symbol="NIFTY", expiry=expiry, step=50, strikes=strikes)
    chain = OptionChainBuilder(state, {"NIFTY:nearest": uni}).build("NIFTY:nearest")
    assert chain is not None and chain.atm_strike == 24000.0

    in_band = [r for r in chain.rows if abs(r.strike - 24000.0) <= 250]
    out_band = [r for r in chain.rows if abs(r.strike - 24000.0) > 250]
    assert out_band, "test universe must extend beyond the IV band"
    assert all(r.ce_iv is None and r.pe_iv is None for r in out_band), "wings must stay None"
    # Rounded premiums on far in-band strikes can land at/below intrinsic
    # (legitimately None); the ATM row itself must always solve.
    atm_row = next(r for r in in_band if r.strike == 24000.0)
    assert atm_row.ce_iv is not None and abs(atm_row.ce_iv - 15.0) < 1.0, atm_row.ce_iv
    assert atm_row.pe_iv is not None and abs(atm_row.pe_iv - 15.0) < 1.0, atm_row.pe_iv
    print(f"  CHAIN  -> ATM IV ce {atm_row.ce_iv} / pe {atm_row.pe_iv}, wings None "
          f"({len(out_band)} rows)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
