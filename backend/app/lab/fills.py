"""Leg resolution + fill pricing, shared by the backtester and the paper
engine so simulated fills obey ONE set of rules everywhere.

Chain access is behind the ChainView protocol: the backtester binds it to
an archive snapshot, the paper loop to the live chain. Strategies never see
the difference — which is what makes paper results comparable to backtests.

Fill honesty rules (mirrors the 22-Jul phantom-fill lesson in app/paper):
  * BUY fills at the ASK (falling back to LTP only when no ask is quoted),
    SELL at the BID — plus configured slippage, always against you.
  * A leg with no fresh quote does not fill; a multi-leg order with any
    unfillable leg is rejected whole (execution risk is logged, not hidden).
  * Strikes must actually exist in the chain at fill time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from app.lab.models import BUY, LegFill, LegSpec
from app.options.iv import bs_price, implied_vol, years_to_expiry

STRIKE_STEP = 50.0            # NIFTY strike interval
TICK = 0.05

# NIFTY lot-size eras. 65 is VERIFIED against the live NFO instrument
# master (NIFTY26SEPFUT, 24-Sep-2026) and against app/config.py's
# closing_lot_size. NSE revises lots by circular and this repo has no
# authoritative record of the pre-2026 sequence, so a single era is honest:
# every dataset a backtest can actually use (chain era 12-Aug-2026 →,
# futures sync ~recent) falls inside it, and if the futures sync ever
# reaches further back, rupee figures there carry today's lot — extend the
# table from a verified circular before trusting those spans.
LOT_SIZE_ERAS = (
    ("2000-01-01", 65),
)


def lot_size_for(day: str) -> int:
    lot = LOT_SIZE_ERAS[0][1]
    for frm, n in LOT_SIZE_ERAS:
        if frm <= day:
            lot = n
    return lot


def snap_tick(px: float) -> float:
    return round(round(px / TICK) * TICK, 2)


@dataclass
class Quote:
    token: int
    tradingsymbol: str
    expiry: str
    strike: float
    right: str
    ltp: float
    bid: Optional[float]
    ask: Optional[float]
    oi: float
    ts: int
    iv: Optional[float] = None


class ChainView:
    """Protocol: as-of option-chain access at one decision instant.
    Implementations: HistChainView (engine), LiveChainView (paper)."""

    ts: int
    spot: float

    def expiry(self, rule: str) -> Optional[str]:
        raise NotImplementedError

    def strikes(self, expiry: str) -> list:
        raise NotImplementedError

    def quote(self, expiry: str, strike: float, right: str
              ) -> Optional[Quote]:
        raise NotImplementedError


def _delta_for(q: Quote, spot: float, now_ts: int) -> Optional[float]:
    """BS delta from the stored/solved IV. Returns signed delta."""
    # chain-archive expiries are ISO strings; years_to_expiry wants a date
    exp = q.expiry
    if isinstance(exp, str):
        exp = date.fromisoformat(exp)
    t = years_to_expiry(exp, now_ts=now_ts)
    if t <= 0:
        return None
    sigma = (q.iv / 100.0) if q.iv else None
    if sigma is None:
        mid = None
        if q.bid and q.ask:
            mid = (q.bid + q.ask) / 2.0
        iv_pct = implied_vol(mid or q.ltp, spot, q.strike, t,
                             q.right == "CE")
        if iv_pct is None:
            return None
        sigma = iv_pct / 100.0
    # numeric delta via bump (bs_price returns premium)
    h = spot * 0.001
    up = bs_price(spot + h, q.strike, t, sigma, q.right == "CE")
    dn = bs_price(spot - h, q.strike, t, sigma, q.right == "CE")
    return (up - dn) / (2 * h)


def resolve_leg(view: ChainView, leg: LegSpec, min_oi: float,
                max_spread_pct: float) -> tuple:
    """-> (Quote | None, reason). Applies strike selection + liquidity
    guards against what was actually listed and quoted."""
    if leg.right == "FUT":
        return None, "resolve_leg is options-only"
    expiry = view.expiry(leg.expiry_rule)
    if not expiry:
        return None, "no_expiry_listed"
    strikes = view.strikes(expiry)
    if not strikes:
        return None, "no_strikes_listed"
    atm = min(strikes, key=lambda s: abs(s - view.spot))

    q: Optional[Quote] = None
    if leg.strike_rule == "ATM_OFF":
        direction = 1.0 if leg.right == "CE" else -1.0
        want = atm + direction * leg.strike_offset * STRIKE_STEP
        if want not in strikes:
            listed = [s for s in strikes
                      if abs(s - want) < STRIKE_STEP / 2]
            if not listed:
                return None, "strike_not_listed"
            want = listed[0]
        q = view.quote(expiry, want, leg.right)
    elif leg.strike_rule == "DELTA":
        best, best_gap = None, 9e9
        lo = atm - 8 * STRIKE_STEP
        hi = atm + 8 * STRIKE_STEP
        for s in strikes:
            if s < lo or s > hi:
                continue
            cand = view.quote(expiry, s, leg.right)
            if not cand:
                continue
            dlt = _delta_for(cand, view.spot, view.ts)
            if dlt is None:
                continue
            gap = abs(abs(dlt) - (leg.delta_target or 0.4))
            if gap < best_gap:
                best, best_gap = cand, gap
        q = best
        if q is None:
            return None, "no_delta_candidate"
    elif leg.strike_rule == "PREMIUM_NEAR":
        best, best_gap = None, 9e9
        for s in strikes:
            cand = view.quote(expiry, s, leg.right)
            if not cand or cand.ltp <= 0:
                continue
            gap = abs(cand.ltp - (leg.premium_target or 0))
            if gap < best_gap:
                best, best_gap = cand, gap
        q = best
        if q is None:
            return None, "no_premium_candidate"
    else:
        return None, "unknown_strike_rule"

    if q is None:
        return None, "no_quote"
    if q.ltp <= 0:
        return None, "no_traded_price"
    if min_oi > 0 and q.oi < min_oi:
        return None, "min_oi"
    if q.bid and q.ask and q.ltp > 0:
        spread_pct = (q.ask - q.bid) / q.ltp * 100.0
        if spread_pct > max_spread_pct:
            return None, "spread_too_wide"
    return q, ""


def fill_price(q: Quote, side: str, slippage_pct: float) -> float:
    """Marketable fill against the book, slippage always against you."""
    if side == BUY:
        base = q.ask if q.ask else q.ltp
        return snap_tick(max(TICK, base * (1 + slippage_pct / 100.0)))
    base = q.bid if q.bid else q.ltp
    return snap_tick(max(TICK, base * (1 - slippage_pct / 100.0)))


def exit_side(side: str) -> str:
    return "SELL" if side == BUY else "BUY"


def leg_fill_from(q: Quote, leg: LegSpec, lots_mult: int, day: str,
                  slippage_pct: float) -> LegFill:
    lot = lot_size_for(day)
    lots = leg.lots * lots_mult
    return LegFill(
        side=leg.side, right=leg.right, tradingsymbol=q.tradingsymbol,
        token=q.token, strike=q.strike, expiry=q.expiry, lots=lots,
        qty=lots * lot, price=fill_price(q, leg.side, slippage_pct),
        quote_ts=q.ts, lot_size=lot)
