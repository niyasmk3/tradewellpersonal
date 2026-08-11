"""Closed-form Black-Scholes Greeks for the Iron Condor module.

Pure functions layered on app.options.iv — same European/no-dividend model,
same 6.5% risk-free leg, same spot-based underlying (the futures basis folds
into the IV; documented bias, consistent across the codebase). Sigma here is a
FRACTION (0.143), not the percentage app.options.iv.implied_vol returns — the
unit trap that already bit the assistant once lives at exactly this boundary,
so `solve_iv` converts at the edge and nothing downstream ever sees a
percentage.
"""
from __future__ import annotations

import math
from datetime import date

from app.options.iv import RISK_FREE, _norm_cdf, implied_vol


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(spot: float, strike: float, t_years: float, sigma: float,
              is_call: bool, r: float = RISK_FREE) -> dict | None:
    """delta / gamma / theta (per calendar day) / vega (per 1 vol-pt).

    None on degenerate inputs — an expired or unpriceable leg has no Greeks,
    and publishing zeros would read as "riskless".
    """
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return None
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / sq
    d2 = d1 - sq
    pdf = _norm_pdf(d1)
    delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
    gamma = pdf / (spot * sq)
    if is_call:
        theta_y = (-spot * pdf * sigma / (2.0 * math.sqrt(t_years))
                   - r * strike * math.exp(-r * t_years) * _norm_cdf(d2))
    else:
        theta_y = (-spot * pdf * sigma / (2.0 * math.sqrt(t_years))
                   + r * strike * math.exp(-r * t_years) * _norm_cdf(-d2))
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta_y / 365.0, 2),
        "vega": round(spot * pdf * math.sqrt(t_years) / 100.0, 2),
    }


def solve_iv(premium: float | None, spot: float | None, strike: float,
             t_years: float, is_call: bool) -> float | None:
    """Implied vol as a FRACTION (0.143), for any strike — not just ATM±5.

    The chain builder's ±5 window exists because wing IVs are numerically
    meaningless for a *regime gauge*; here the number prices a leg we intend
    to trade, so we solve wherever the bisection converges and let its own
    refusal (premium ≤ intrinsic, > 500%-vol price) be the quality filter.
    """
    pct = implied_vol(premium, spot, strike, t_years, is_call)
    return pct / 100.0 if pct is not None else None


def prob_above(spot: float, strike: float, t_years: float, sigma: float,
               r: float = RISK_FREE) -> float | None:
    """Risk-neutral P(S_T > strike) = N(d2). Market-implied odds, not a forecast."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return None
    sq = sigma * math.sqrt(t_years)
    d2 = (math.log(spot / strike) + (r - 0.5 * sigma * sigma) * t_years) / sq
    return _norm_cdf(d2)


def condor_pop(spot: float, be_low: float, be_high: float, t_years: float,
               sigma_low: float, sigma_high: float) -> float | None:
    """P(BE_low < S_T < BE_high), each tail priced with its own side's IV.

    Using the short-put IV below and the short-call IV above respects the skew
    the market is actually quoting instead of averaging it away.
    """
    p_above_low = prob_above(spot, be_low, t_years, sigma_low)
    p_above_high = prob_above(spot, be_high, t_years, sigma_high)
    if p_above_low is None or p_above_high is None:
        return None
    return max(0.0, min(1.0, p_above_low - p_above_high))
