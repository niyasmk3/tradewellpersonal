"""Zerodha NSE F&O option-buy charges — the Python mirror of lib/tradeMath.ts.

Paper P&L is worth nothing if it is gross. On a cheap contract round-trip
charges reach ~17% of the risk leg, which is the difference between a strategy
that looks profitable and one that is not. So the simulator books the same
costs a real fill would.

The rates here MUST stay identical to CHARGE_RATES in frontend/lib/tradeMath.ts.
tests/test_paper.py pins the same worked example the TypeScript suite pins
(Rs 75.78 round trip, Rs 26.98 lapse), so the two cannot drift apart silently.
"""
from __future__ import annotations

BROKERAGE_PER_ORDER = 20.0      # flat, per executed order
STT_SELL_PCT = 0.0015           # 0.15% of SELL premium turnover (w.e.f. 01-Apr-2026)
EXCHANGE_TXN_PCT = 0.0003503    # NSE options, premium turnover, both sides
IPFT_PCT = 0.000005             # NSE investor protection fund, Rs 50/crore
SEBI_PCT = 0.000001             # Rs 10 per crore
GST_PCT = 0.18                  # on brokerage + exchange + SEBI + IPFT only
STAMP_DUTY_BUY_PCT = 0.00003    # 0.003%, buy side only


def charges(entry: float, exit_premium: float, qty: int, legs: int = 2) -> float:
    """Total charges for the round trip (legs=2) or a lapse (legs=1).

    An option left to expire worthless is never sold: one brokerage leg and no
    STT, since STT falls on the sell side.
    """
    if qty <= 0 or entry <= 0:
        return 0.0
    buy_turnover = entry * qty
    sell_turnover = exit_premium * qty if legs == 2 else 0.0
    turnover = buy_turnover + sell_turnover

    brokerage = BROKERAGE_PER_ORDER * legs
    stt = sell_turnover * STT_SELL_PCT
    exch = turnover * EXCHANGE_TXN_PCT
    ipft = turnover * IPFT_PCT
    sebi = turnover * SEBI_PCT
    gst = (brokerage + exch + ipft + sebi) * GST_PCT
    stamp = buy_turnover * STAMP_DUTY_BUY_PCT
    return round(brokerage + stt + exch + ipft + sebi + gst + stamp, 2)


def net_pnl(entry: float, exit_premium: float, qty: int) -> float:
    """Realised P&L after charges — what the trade actually returned."""
    gross = (exit_premium - entry) * qty
    return round(gross - charges(entry, exit_premium, qty, 2), 2)
