"""Per-strike implied volatility — Black-Scholes inverted by bisection.

Kite ticks carry price/OI/volume but not IV, so we compute it: given the
option's live premium, spot, strike and time to expiry, find the sigma whose
Black-Scholes price reproduces the premium. Bisection is deliberate — it is
~60 evaluations of a closed-form price, immune to the vega≈0 flat spots that
make Newton's method diverge on deep ITM/OTM strikes near expiry.

Only near-ATM strikes are worth the answer (deep wings quote at tick-size
premiums where IV is numerically meaningless), so the chain builder asks for
±5 strikes around ATM and leaves the rest None.

India specifics: NIFTY options are European (BS applies exactly), expiry
settles at the 15:30 close, and the risk-free leg uses ~6.5% (91-day T-bill
neighbourhood). Precision beyond that is noise at our use — the number is a
regime gauge (skew, rich/cheap vs VIX), not a pricing engine.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone

RISK_FREE = 0.065
_IST = timezone(timedelta(hours=5, minutes=30))
_YEAR_S = 365.0 * 86400.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, sigma: float,
             is_call: bool, r: float = RISK_FREE) -> float:
    """Black-Scholes price of a European option (no dividends)."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / sq
    d2 = d1 - sq
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(premium: float | None, spot: float | None, strike: float,
                t_years: float, is_call: bool, r: float = RISK_FREE) -> float | None:
    """IV as a percentage (e.g. 14.3), or None when no vol explains the premium.

    None is an honest answer, not a failure: a premium at/below intrinsic has
    zero time value (stale quote or arbitrage print), and one above the 500%%-vol
    price is a bad tick. Publishing a number for either would be fiction.
    """
    if premium is None or spot is None or premium <= 0 or spot <= 0 \
            or strike <= 0 or t_years <= 0:
        return None
    lo, hi = 1e-3, 5.0
    if premium <= bs_price(spot, strike, t_years, lo, is_call, r):
        return None
    if premium >= bs_price(spot, strike, t_years, hi, is_call, r):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if bs_price(spot, strike, t_years, mid, is_call, r) < premium:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0 * 100.0, 1)


def years_to_expiry(expiry: date | None, now_ts: float | None = None) -> float:
    """ACT/365 time to the 15:30 IST settlement on expiry day. 0.0 if past."""
    if expiry is None:
        return 0.0
    now = time.time() if now_ts is None else now_ts
    settle = datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=_IST)
    return max(settle.timestamp() - now, 0.0) / _YEAR_S
