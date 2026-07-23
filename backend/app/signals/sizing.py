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

Worked example (the one this was specified with): fund ₹10,00,000, price
₹100, lot size 65 → 10,00,000 ÷ (100 × 65) = 153.84… → 153 lots → 9,945 qty.
Fractional lots always round DOWN, and the price used is the LIMIT the basket
sends (entry_high — see apply_fund_sizing): the fund is a ceiling, not a
target, and a ceiling is measured at the worst fill the order can take.
"""
from __future__ import annotations

from app.signals.models import SignalCard


def apply_fund_sizing(card: SignalCard, fund: float) -> None:
    """Attach fund_lots / fund_qty / fund_note to `card`, or clear them.

    COST BASIS IS entry_high, not the last trade. The order this prefill feeds
    is a BUY LIMIT at the top of the entry zone, and a resting limit may
    legally fill anywhere up to its price — so entry_high is the most a lot can
    cost. Dividing by a live premium below it counts lots the worst legal fill
    cannot pay for (at a 10-lakh fund the gap is ~3%, which at the broker is a
    margin rejection, not a rounding error). The fund is a ceiling; affordable
    means affordable at the worst fill the order permits. live_premium /
    ref_entry_premium are only fallbacks for zoneless cards.

    Clears the fields when inputs are missing so a re-poll that loses its
    inputs cannot leave a stale quantity on the card.
    """
    card.fund_lots = card.fund_qty = None
    card.fund_note = None

    lot = card.lot_size or 0
    premium = card.entry_high or card.live_premium or card.ref_entry_premium
    if fund <= 0 or lot <= 0 or premium is None or premium <= 0:
        return

    per_lot_cost = premium * lot
    lots = int(fund // per_lot_cost)
    card.fund_lots = lots
    card.fund_qty = lots * lot
    if lots <= 0:
        card.fund_note = (
            f"Fund ₹{fund:,.0f} does not cover one lot "
            f"(₹{per_lot_cost:,.0f} at limit ₹{premium:g})"
        )
        return
    card.fund_note = (
        f"₹{fund:,.0f} buys {lots} lot(s) = {lots * lot:,} qty "
        f"≈ ₹{lots * per_lot_cost:,.0f} at limit ₹{premium:g}"
    )
