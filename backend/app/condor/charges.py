"""Zerodha NSE F&O charges for a 4-leg iron condor.

Same schedule constants as app/paper/charges.py (which stays single-leg
long-option and is deliberately not overloaded — its docstring guards the
"every Tradewell signal is a BOUGHT option" invariant). A condor is 8 executed
orders round-trip: entry = SELL 2 shorts + BUY 2 wings, exit = BUY 2 shorts
back + SELL 2 wings. STT applies to SELL premium turnover only; stamp duty to
BUY turnover only. Legs that lapse worthless at expiry have no exit order and
therefore no exit brokerage/charges.
"""
from __future__ import annotations

BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT = 0.0015
EXCHANGE_TXN_PCT = 0.0003503
IPFT_PCT = 0.000005
SEBI_PCT = 0.000001
GST_PCT = 0.18
STAMP_DUTY_BUY_PCT = 0.00003


def _order(premium: float, qty: int, is_sell: bool) -> float:
    """All-in cost of one executed option order at `premium` for `qty` units."""
    if premium <= 0 or qty <= 0:
        return 0.0
    turnover = premium * qty
    exch = turnover * (EXCHANGE_TXN_PCT + IPFT_PCT + SEBI_PCT)
    gst = (BROKERAGE_PER_ORDER + exch) * GST_PCT
    stt = turnover * STT_SELL_PCT if is_sell else 0.0
    stamp = turnover * STAMP_DUTY_BUY_PCT if not is_sell else 0.0
    return BROKERAGE_PER_ORDER + exch + gst + stt + stamp


def entry_charges(short_ce: float, short_pe: float, wing_ce: float,
                  wing_pe: float, qty: int) -> float:
    """4 entry orders: sell both shorts, buy both wings."""
    return round(
        _order(short_ce, qty, True) + _order(short_pe, qty, True)
        + _order(wing_ce, qty, False) + _order(wing_pe, qty, False), 2)


def exit_charges(short_ce: float, short_pe: float, wing_ce: float,
                 wing_pe: float, qty: int) -> float:
    """4 exit orders: buy shorts back, sell wings. Pass 0.0 for a leg that
    lapsed worthless (no order, no charge)."""
    return round(
        _order(short_ce, qty, False) + _order(short_pe, qty, False)
        + _order(wing_ce, qty, True) + _order(wing_pe, qty, True), 2)


def roundtrip_estimate(credit: float, width: float, qty: int) -> float:
    """Planning estimate before real fills exist. Wings typically quote ~35% of
    their short, so shorts_total = credit / (1 - 0.35); exit assumed at half
    the entry premiums (a mid-life close). Used for the credit-vs-friction
    floor only; the monitor recomputes with real premiums."""
    if qty <= 0 or credit <= 0:
        return 0.0
    shorts_total = credit / 0.65
    s_ce, s_pe = shorts_total * 0.55, shorts_total * 0.45
    w_ce, w_pe = s_ce * 0.35, s_pe * 0.35
    return round(entry_charges(s_ce, s_pe, w_ce, w_pe, qty)
                 + exit_charges(s_ce * 0.5, s_pe * 0.5, w_ce * 0.5, w_pe * 0.5, qty), 2)
