"""Expected move to expiry — four estimators, all shown, none blended.

Straddle and IV are what the market charges; ATR and realized vol are what the
underlying has actually delivered. When they disagree, that disagreement is the
finding (rich or cheap premium), so the engine reports all four and takes
max(straddle, IV) as the conservative safety yardstick for strike distance.

The realized-vol leg is the codebase's first HV computation (the Phase-1 audit
confirmed none existed): close-to-close stdev of daily log returns over 20
sessions, annualized by sqrt(252), off the Patterns Module's 5-min spine.
Sourced best-effort — a missing spine degrades to None, never to a guess.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date, timedelta

from app.market import calendar as mcal
from app.models.schemas import OptionChain

_IST_OFFSET = 19800
_CACHE_S = 300.0

_daily_cache: dict = {"at": 0.0, "rows": None}


def _daily_bars(min_days: int = 26) -> list[dict] | None:
    """Day-aggregated OHLC from the patterns 5-min spine (oldest first)."""
    now = time.time()
    if _daily_cache["rows"] is not None and now - _daily_cache["at"] < _CACHE_S:
        return _daily_cache["rows"]
    try:
        from app.patterns import store as pstore
        df = pstore.load_tail(min_days * 80)      # ~75 bars/session + slack
        if df is None or len(df) < 75:
            return None
        df = df.copy()
        df["day"] = (df["ts"] + _IST_OFFSET) // 86400
        rows = []
        for day, g in df.groupby("day", sort=True):
            rows.append({
                "day": int(day),
                "open": float(g["open"].iloc[0]),
                "high": float(g["high"].max()),
                "low": float(g["low"].min()),
                "close": float(g["close"].iloc[-1]),
            })
        _daily_cache.update(at=now, rows=rows)
        return rows
    except Exception:
        return None


def hv20() -> float | None:
    """Annualized 20-day close-to-close realized vol, as a fraction."""
    rows = _daily_bars()
    if not rows or len(rows) < 21:
        return None
    closes = [r["close"] for r in rows[-21:]]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 15:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var) * math.sqrt(252.0), 4)


def daily_atr14() -> float | None:
    """Wilder-style ATR(14) on day-aggregated bars, in index points."""
    rows = _daily_bars()
    if not rows or len(rows) < 16:
        return None
    trs = []
    for prev, cur in zip(rows, rows[1:]):
        trs.append(max(cur["high"] - cur["low"],
                       abs(cur["high"] - prev["close"]),
                       abs(cur["low"] - prev["close"])))
    atr = sum(trs[:14]) / 14.0
    for tr in trs[14:]:
        atr = (atr * 13.0 + tr) / 14.0
    return round(atr, 2)


def trading_days_to(expiry: date | None) -> int:
    if expiry is None:
        return 0
    d = mcal.now_ist().date()
    n = 0
    while d < expiry:
        d = d + timedelta(days=1)
        if mcal.is_trading_day(d):
            n += 1
    return n


@dataclass
class ExpectedMove:
    straddle: float | None = None    # ATM CE + PE premium, points
    iv_based: float | None = None    # S * sigma_atm * sqrt(T), points
    atr_based: float | None = None   # daily ATR14 * sqrt(trading days), points
    rv_based: float | None = None    # S * HV20 * sqrt(T), points
    sigma_atm: float | None = None   # fraction
    hv20: float | None = None        # fraction
    dte_trading: int = 0

    @property
    def primary(self) -> float | None:
        """Conservative safety EM: the larger of what the market charges."""
        vals = [v for v in (self.straddle, self.iv_based) if v]
        return round(max(vals), 1) if vals else None

    @property
    def iv_over_rv(self) -> float | None:
        if self.sigma_atm and self.hv20 and self.hv20 > 0:
            return round(self.sigma_atm / self.hv20, 2)
        return None


def compute(chain: OptionChain | None, spot: float | None, t_years: float,
            expiry: date | None) -> ExpectedMove:
    em = ExpectedMove(dte_trading=trading_days_to(expiry))
    if chain and chain.atm_strike and chain.rows:
        atm_row = next((r for r in chain.rows if r.strike == chain.atm_strike), None)
        if atm_row and atm_row.ce_ltp and atm_row.pe_ltp:
            em.straddle = round(atm_row.ce_ltp + atm_row.pe_ltp, 1)
        ivs = [v for v in ((atm_row.ce_iv if atm_row else None),
                           (atm_row.pe_iv if atm_row else None)) if v]
        if ivs:
            em.sigma_atm = round(sum(ivs) / len(ivs) / 100.0, 4)  # chain IV is a pct
    if spot and em.sigma_atm and t_years > 0:
        em.iv_based = round(spot * em.sigma_atm * math.sqrt(t_years), 1)
    atr_d = daily_atr14()
    if atr_d and em.dte_trading > 0:
        em.atr_based = round(atr_d * math.sqrt(em.dte_trading), 1)
    em.hv20 = hv20()
    if spot and em.hv20 and t_years > 0:
        em.rv_based = round(spot * em.hv20 * math.sqrt(t_years), 1)
    return em
