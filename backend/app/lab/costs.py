"""Date-aware Indian F&O transaction costs + margin ESTIMATES for the Lab.

Option rates mirror the audited constants in app/paper/charges.py and
app/condor/charges.py (pinned by tests there); futures rates are added here
with sources. Rates are looked up by trade date so historical backtests use
the rates of their era; today only one era is recorded — extend RATES when
a circular changes something, never edit an old era in place.

MARGINS ARE ESTIMATES. There is no SPAN feed in this repo. The formulas
below approximate NSE index-option margining closely enough for capacity
and utilisation displays, and every consumer must label them "est.".
"""
from __future__ import annotations

from typing import Optional

# --- statutory + brokerage rate eras --------------------------------------
# One dict per era, ascending by effective date (ISO). Values are fractions
# of turnover unless suffixed _rs.
RATES = (
    {
        "from": "2026-04-01",
        # options (premium turnover)
        "opt_stt_sell": 0.0015,        # STT, sell side (w.e.f. 01-Apr-2026)
        "opt_exch": 0.0003503,         # NSE transaction charge
        "opt_ipft": 0.000005,          # investor protection fund
        "opt_stamp_buy": 0.00003,      # stamp duty, buy side
        # futures (contract turnover)
        "fut_stt_sell": 0.0002,        # STT futures, sell side (Oct-2024 rate)
        "fut_exch": 0.0000173,         # NSE transaction charge, futures
        "fut_ipft": 0.000001,
        "fut_stamp_buy": 0.00002,
        # common
        "sebi": 0.000001,              # ₹10 / crore
        "gst": 0.18,                   # on brokerage + exch + sebi + ipft
        "brokerage_per_order_rs": 20.0,
    },
)


def rates_for(day: str) -> dict:
    """day: ISO date. Latest era at or before the date; the earliest era
    also covers anything older (better than refusing to cost a backtest,
    and the difference is disclosed in the run's cost_model snapshot)."""
    chosen = RATES[0]
    for era in RATES:
        if era["from"] <= day:
            chosen = era
    return chosen


def order_cost(kind: str, side: str, price: float, qty: int,
               day: str) -> float:
    """All-in statutory + brokerage cost of ONE order.
    kind: "OPT" | "FUT"; side: "BUY" | "SELL"; price per unit; qty units.
    """
    r = rates_for(day)
    turnover = abs(price) * qty
    if kind == "OPT":
        stt = turnover * r["opt_stt_sell"] if side == "SELL" else 0.0
        exch = turnover * r["opt_exch"]
        ipft = turnover * r["opt_ipft"]
        stamp = turnover * r["opt_stamp_buy"] if side == "BUY" else 0.0
    else:
        stt = turnover * r["fut_stt_sell"] if side == "SELL" else 0.0
        exch = turnover * r["fut_exch"]
        ipft = turnover * r["fut_ipft"]
        stamp = turnover * r["fut_stamp_buy"] if side == "BUY" else 0.0
    sebi = turnover * r["sebi"]
    gst = (r["brokerage_per_order_rs"] + exch + sebi + ipft) * r["gst"]
    return round(r["brokerage_per_order_rs"] + stt + exch + ipft + sebi
                 + stamp + gst, 2)


def cost_model_snapshot(day: str, slippage_note: str = "") -> dict:
    """What a run stores so its costs are reproducible."""
    r = dict(rates_for(day))
    r["slippage"] = slippage_note
    return r


# --- margin estimates ------------------------------------------------------

FUT_MARGIN_PCT = 0.12          # ≈ SPAN+exposure for NIFTY futures (est.)
SHORT_OPT_BASE_PCT = 0.15      # short option: max(base·spot − OTM, floor·spot)
SHORT_OPT_FLOOR_PCT = 0.075
PAIR_OFFSET_FACTOR = 0.70      # straddle/strangle cross-margin benefit (est.)


def futures_margin_rs(price: float, qty: int) -> float:
    return round(price * qty * FUT_MARGIN_PCT, 0)


def short_option_margin_rs(spot: float, strike: float, premium: float,
                           qty: int, right: str) -> float:
    otm = max(0.0, (strike - spot) if right == "CE" else (spot - strike))
    per_unit = premium + max(SHORT_OPT_BASE_PCT * spot - otm,
                             SHORT_OPT_FLOOR_PCT * spot)
    return round(per_unit * qty, 0)


def position_margin_rs(legs: list, spot: float) -> float:
    """Estimate for a whole multi-leg position.
    legs: dicts with side, right, strike, price, qty.
    Defined-risk verticals (short + long same right) margin at max loss;
    naked shorts at the short-option formula; short pairs get the
    cross-margin factor; long options at premium paid."""
    longs = [l for l in legs if l["side"] == "BUY" and l["right"] in ("CE", "PE")]
    shorts = [l for l in legs if l["side"] == "SELL" and l["right"] in ("CE", "PE")]
    futs = [l for l in legs if l["right"] == "FUT"]

    total = 0.0
    for f in futs:
        total += futures_margin_rs(f["price"], f["qty"])
    for l in longs:
        total += l["price"] * l["qty"]

    covered: set = set()
    for s_i, s in enumerate(shorts):
        wing = None
        for l_i, l in enumerate(longs):
            if l["right"] == s["right"] and l_i not in covered:
                wing = (l_i, l)
                break
        if wing is not None:
            covered.add(wing[0])
            width = abs(wing[1]["strike"] - s["strike"])
            total += width * s["qty"]        # defined risk: max loss
        else:
            total += short_option_margin_rs(spot, s["strike"], s["price"],
                                            s["qty"], s["right"])
    if len(shorts) >= 2 and not covered:
        total *= PAIR_OFFSET_FACTOR
    return round(total, 0)
