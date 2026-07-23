"""Fund-affordability sizing + the premium freshness gate.

Two features, one failure mode: a quantity or a price shown to the user that
the market never offered. The fund prefill must never claim lots the money
cannot buy, and the freshness gate must never let a card be priced on a quote
from a market that no longer exists (observed 21/22-Jul: refs 3 min to a full
DAY stale, entry zones unfillable).

Run:  python backend/tests/test_fund_sizing.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.signals.engine import premium_quote
from app.signals.models import (
    Action, Direction, ScoreBreakdown, SignalCard, SignalState, TradingMode,
)
from app.signals.sizing import apply_fund_sizing

NOW = 1_784_700_000


def _card(**over):
    base = dict(
        id="S1", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_PE, direction=Direction.PE, state=SignalState.ACTIVE,
        contract="NIFTY 24000 PE", strike=24000.0, token=999, expiry="2026-07-28",
        entry_low=99.0, entry_high=102.0, premium_sl=82.0, target1=127.0,
        target2=152.0, trailing_sl_rule="r", risk_reward=1.5, confidence=85.0,
        underlying_invalidation="u", invalidation_note="n",
        invalidation_level=24100.0, invalidation_dir="below",
        created_at=NOW, valid_until=NOW + 480,
        score=ScoreBreakdown(direction=Direction.PE, components=[], total=85.0, max=100),
        lot_size=65, ref_entry_premium=100.0,
    )
    base.update(over)
    return SignalCard(**base)


# --- the worked example the feature was specified with ------------------------

def test_fund_buys_the_specified_lot_count():
    """₹10,00,000 at a ₹100 limit, lot 65 → 153 lots → 9,945 qty."""
    card = _card(entry_high=100.0)           # the LIMIT the basket sends
    apply_fund_sizing(card, 1_000_000.0)
    assert card.fund_lots == 153, card.fund_lots
    assert card.fund_qty == 9_945, card.fund_qty
    assert "153" in card.fund_note and "9,945" in card.fund_note, card.fund_note
    print(f"  FUND   -> ₹10,00,000 @ limit ₹100×65 = {card.fund_lots} lots / {card.fund_qty} qty")


def test_fund_rounds_down_never_up():
    """The fund is a ceiling: 154 lots costs ₹10,01,000, which we don't have."""
    card = _card(entry_high=100.0)
    apply_fund_sizing(card, 1_000_999.0)     # one rupee short of the next lot
    assert card.fund_lots == 153, card.fund_lots
    apply_fund_sizing(card, 1_001_000.0)     # exactly the next lot
    assert card.fund_lots == 154, card.fund_lots
    print("  FUND   -> fractional lots always floor; exact boundary buys the lot")


def test_cost_basis_is_the_limit_not_the_last_trade():
    """A resting LIMIT at entry_high may legally fill there, so affordability
    is measured at entry_high even while the tape prints lower — dividing by
    the live ₹98 would claim 156 lots the worst legal fill cannot pay for."""
    card = _card(entry_high=102.0, live_premium=98.0)
    apply_fund_sizing(card, 1_000_000.0)
    assert card.fund_lots == 150, card.fund_lots      # 1,000,000 // (102*65)
    assert card.fund_qty == 150 * 65
    # Zoneless card (no entry_high): the tape is all there is.
    zoneless = _card(entry_high=0.0, live_premium=130.0)
    apply_fund_sizing(zoneless, 1_000_000.0)
    assert zoneless.fund_lots == 118, zoneless.fund_lots   # 1,000,000 // (130*65)
    print("  FUND   -> limit ₹102 outranks live ₹98 (150 lots); zoneless uses the tape")


def test_no_fund_or_no_premium_clears_the_fields():
    card = _card(live_premium=None)
    apply_fund_sizing(card, 0.0)
    assert card.fund_lots is None and card.fund_qty is None and card.fund_note is None

    # A previously-populated card must be CLEARED when inputs vanish — a stale
    # quantity left on the card is exactly the bug this feature must not have.
    card2 = _card(entry_high=100.0)
    apply_fund_sizing(card2, 1_000_000.0)
    assert card2.fund_lots == 153
    card2.entry_high = 0.0
    card2.ref_entry_premium = None
    apply_fund_sizing(card2, 1_000_000.0)
    assert card2.fund_lots is None and card2.fund_note is None
    print("  FUND   -> unset fund/premium leaves no fields, and clears stale ones")


def test_fund_too_small_for_one_lot_says_so():
    card = _card(entry_high=100.0)
    apply_fund_sizing(card, 5_000.0)         # one lot costs ₹6,500
    assert card.fund_lots == 0 and card.fund_qty == 0
    assert "does not cover" in card.fund_note, card.fund_note
    print("  FUND   -> ₹5,000 < one ₹6,500 lot: 0 lots, note explains why")


# --- premium freshness --------------------------------------------------------

def test_fresh_tick_overrides_a_stale_chain_price():
    """The chain froze at 143.15 while the tape traded 155 (22-Jul, candle-
    verified). The freshest source must win."""
    ticks = {999: {"last_price": 155.45, "ts": NOW - 3}}
    px, age = premium_quote(ticks, 999, 143.15, NOW)
    assert px == 155.45 and age == 3, (px, age)
    print("  FRESH  -> tick ₹155.45 (3s) outranks chain ₹143.15")


def test_dead_tick_stream_reports_its_age():
    """A tick carrying yesterday's exchange stamp must age by a day, not look
    current — this is the 09:15:03 previous-close card."""
    ticks = {999: {"last_price": 197.45, "ts": NOW - 63_000}}
    px, age = premium_quote(ticks, 999, 197.45, NOW)
    assert age == 63_000, age
    print("  FRESH  -> yesterday's tick ages 63,000s; the gate can refuse it")


def test_missing_tick_or_timestamp_is_unverifiable():
    px, age = premium_quote({}, 999, 120.0, NOW)
    assert px == 120.0 and age is None
    px, age = premium_quote({999: {"last_price": 121.0}}, 999, 120.0, NOW)
    assert px == 121.0 and age is None
    px, age = premium_quote(None, 999, 120.0, NOW)
    assert px == 120.0 and age is None
    print("  FRESH  -> no tick / no ts → age None (caller refuses to price)")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
