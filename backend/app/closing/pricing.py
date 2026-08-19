"""Modelled ATM premium: real spot + real VIX -> Black-Scholes price.

This is the study's modelled half and it is the half to distrust. It exists
only because real premiums are unobtainable (Kite drops expired option tokens),
and it makes exactly four assumptions, each isolated here so validate.py can
put a number on the damage:

  1. NIFTY options are European and Black-Scholes prices them. True by
     construction — NIFTY options ARE European and cash-settled.
  2. The underlying is the forward, spot grown at a carry rate fitted from
     real put-call parity (~11-12%/yr, not the 6.5% risk-free).
  3. The weekly ATM implied vol is VIX times a DTE-dependent ratio.
  4. Calls and puts share that vol — no skew. A real ATM put carries a small
     vol premium over the call, so directional results are marginally
     optimistic for PE trades and pessimistic for CE trades.

(2) and (3) are measured in calibration.py over six sessions at VIX~11, which
is the binding limitation on everything Layer 2 reports.

Black-Scholes itself is app.options.iv.bs_price — one implementation in the
codebase, already pinned by tests.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, Optional, Sequence

from app.closing import calibration
from app.closing.calendar import settlement_ts
from app.options.iv import RISK_FREE, bs_price

STRIKE_STEP = 50.0   # NIFTY option strikes
_YEAR_S = 365.0 * 86400.0

# Used only when no calibration sample exists at all (fresh clone, empty condor
# DB). Deliberately flat and deliberately wrong near expiry — a study running
# on this should say so rather than quietly look calibrated.
FALLBACK_CURVE = [{"dte_mid": 0.0, "ratio": 1.0}, {"dte_mid": 30.0, "ratio": 1.0}]


def atm_strike(spot: float, step: float = STRIKE_STEP) -> float:
    return round(spot / step) * step


class IvCurve:
    """Piecewise-linear DTE -> (ATM IV / VIX) ratio, flat outside the sample.

    Flat extrapolation, not linear: extending the measured slope past the last
    observation would have the ratio drift without evidence, and beyond ~12 DTE
    this study has none.
    """

    def __init__(self, points: Optional[Sequence[dict]] = None) -> None:
        raw = list(points) if points else list(FALLBACK_CURVE)
        pts = sorted(
            ((float(p.get("dte_mid", p.get("dte", 0.0))), float(p["ratio"])) for p in raw),
            key=lambda t: t[0],
        )
        self.points = pts or [(0.0, 1.0)]
        self.is_fallback = not points

    def ratio(self, dte: float) -> float:
        pts = self.points
        if dte <= pts[0][0]:
            return pts[0][1]
        if dte >= pts[-1][0]:
            return pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= dte <= x1:
                if x1 == x0:
                    return y1
                return y0 + (dte - x0) / (x1 - x0) * (y1 - y0)
        return pts[-1][1]

    def sigma(self, vix_pct: float, dte: float) -> float:
        """Implied vol as a FRACTION, the unit bs_price expects."""
        return max(vix_pct * self.ratio(dte) / 100.0, 1e-4)


def dte_days(ts: float, expiry: date) -> float:
    """Calendar days from `ts` to the 15:30 IST settlement on `expiry`."""
    return (settlement_ts(expiry) - float(ts)) / 86400.0


class OptionModel:
    """Carry + vol curve, bundled so no caller can use one without the other.

    Pairing matters: the vol ratios were solved against the fitted forward, so
    applying them at a different carry silently reintroduces the bias the
    two-stage fit removed.
    """

    def __init__(self, curve: IvCurve, carry: Optional[float] = None) -> None:
        self.curve = curve
        self.carry = RISK_FREE if carry is None else float(carry)
        self.carry_is_fitted = carry is not None

    def premium(self, spot: float, strike: float, ts: float, expiry: date,
                is_call: bool, vix_pct: float) -> Optional[float]:
        """Modelled mid premium. None when the contract has already settled —
        an expired leg has no mid, and returning intrinsic here would silently
        turn a modelling gap into a fabricated fill."""
        dte = dte_days(ts, expiry)
        if dte <= 0 or spot <= 0 or strike <= 0 or vix_pct <= 0:
            return None
        t_years = dte * 86400.0 / _YEAR_S
        sigma = self.curve.sigma(vix_pct, dte)
        return round(bs_price(spot, strike, t_years, sigma, is_call, self.carry), 2)

    def describe(self) -> dict:
        return {
            "carry_pct": round(self.carry * 100, 2),
            "carry_fitted": self.carry_is_fitted,
            "curve_points": [{"dte": x, "ratio": round(y, 3)} for x, y in self.curve.points],
            "curve_is_fallback": self.curve.is_fallback,
        }


def build_model(spot_at: Callable[[int], Optional[float]],
                db_path=calibration.CHAIN_DB,
                exclude_sessions: Optional[set] = None) -> tuple:
    """(OptionModel, fit_report). Carry first, then the vol curve on top of it.

    `exclude_sessions` is passed straight through to the fits so a caller can
    build a model that has never seen a given session — the leave-one-out
    machinery in validate.py, and the only way its error number means anything.
    """
    carry_fit = calibration.fit_carry(spot_at, db_path, exclude_sessions)
    carry = carry_fit.get("rate")
    iv_fit = calibration.fit_iv_curve(spot_at, carry if carry else RISK_FREE,
                                      db_path, exclude_sessions)
    model = OptionModel(IvCurve(iv_fit["points"] or None), carry)
    return model, {"carry": carry_fit, "iv_curve": iv_fit,
                   "spread": calibration.fit_spread(db_path)}
