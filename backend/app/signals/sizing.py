"""Affordability sizing: how many lots does today's fund actually buy?

A separate question from risk sizing, and the distinction is the whole point:

  * suggested_lots (service._apply_sizing) answers "at my risk budget, how many
    lots may I afford to LOSE on" — it divides a loss budget by the per-lot
    stop distance.
  * fund_lots (here) answers "at this premium, how many lots can my fund BUY"
    — it divides the deployable fund by the per-lot premium outlay.

The two can differ by an order of magnitude, and the user types the trade into
Kite from one number. Computing both, labelling both, and prefilling the
conservative one keeps the fast path honest.

Worked example (the one this was specified with): fund ₹10,00,000, premium
₹100, lot size 65 → 10,00,000 ÷ (100 × 65) = 153.84… → 153 lots → 9,945 qty.
Fractional lots always round DOWN: the fund is a ceiling, not a target.
"""
from __future__ import annotations

from app.signals.models import SignalCard


def apply_fund_sizing(card: SignalCard, fund: float) -> None:
    """Attach fund_lots / fund_qty / fund_note to `card`, or clear them.

    Premium preference: live_premium (attached at request time, ticks with the
    tape) over ref_entry_premium (issue-time reference). entry_high is NOT a
    fallback — prefilling a quantity from a price nobody quoted invents money.
    Clears the fields when inputs are missing so a re-poll that loses the live
    tick cannot leave a stale quantity on the card.
    """
    card.fund_lots = card.fund_qty = None
    card.fund_note = None

    lot = card.lot_size or 0
    premium = card.live_premium or card.ref_entry_premium
    if fund <= 0 or lot <= 0 or premium is None or premium <= 0:
        return

    per_lot_cost = premium * lot
    lots = int(fund // per_lot_cost)
    card.fund_lots = lots
    card.fund_qty = lots * lot
    if lots <= 0:
        card.fund_note = (
            f"Fund ₹{fund:,.0f} does not cover one lot "
            f"(₹{per_lot_cost:,.0f} at ₹{premium:g})"
        )
        return
    card.fund_note = (
        f"₹{fund:,.0f} buys {lots} lot(s) = {lots * lot:,} qty "
        f"≈ ₹{lots * per_lot_cost:,.0f} at ₹{premium:g}"
    )
