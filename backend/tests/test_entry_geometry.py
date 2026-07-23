"""Entry-time geometry: the ladder is priced from YOUR fill, and size is guarded.

Both behaviours here are direct consequences of 22-Jul, when the journal
inherited levels drawn for a premium nobody paid:

  * a card referenced at 197.45 was filled at 289.70 and kept the card's stop —
    Rs 9,847 at risk against a sizing note that said Rs 3,845, and a 0.17
    reward:risk on a setup advertised at 2.0;
  * mixing anchors (stop from the card, early-target from the fill) put
    quick_target ABOVE target1, so the monitor called an "early" book after T1;
  * ten lots were journalled against a card suggesting three, putting 31% of the
    daily loss limit on one trade with nothing asking whether that was intended.

Run:  python backend/tests/test_entry_geometry.py
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException

from app.api.routes_trades import _guard_size
from app.config import Settings
from app.signals.models import (
    Action, Direction, ScoreBreakdown, SignalCard, SignalState, TradingMode,
)
from app.trades.models import EnterRequest
from app.trades.store import TradeStore

# Intraday defaults: 18% premium stop, targets at 1.5R and 2.5R, early book +12%.
SL_PCT, RR1, RR2, QUICK = 0.18, 1.5, 2.5, 0.12


def _cfg(**over):
    base = {"SIGNAL_DAILY_LOSS_LIMIT": 50000.0}
    base.update(over)
    return Settings(_env_file=None, **base)


def _store(name="/tmp/tw-entry-geometry-test.json"):
    p = pathlib.Path(name)
    p.unlink(missing_ok=True)
    return TradeStore(path=p)


def _card(**over):
    """A card priced around a ~197 premium — the 22-Jul positional shape."""
    base = dict(
        id="S1", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_PE, direction=Direction.PE, state=SignalState.ACTIVE,
        contract="NIFTY 24250 PE", strike=24250.0, token=999, expiry="2026-07-28",
        entry_low=195.5, entry_high=201.4, premium_sl=161.9, target1=215.15,
        target2=286.1, trailing_sl_rule="r", risk_reward=2.0, confidence=75.0,
        underlying_invalidation="u", invalidation_note="n",
        invalidation_level=24300.0, invalidation_dir="below",
        created_at=1_700_000_000, valid_until=1_700_000_600,
        score=ScoreBreakdown(direction=Direction.PE, components=[], total=75.0, max=100),
        lot_size=65, ref_entry_premium=197.45, suggested_lots=3,
    )
    base.update(over)
    return SignalCard(**base)


# --- the ladder follows the fill ---------------------------------------------

def test_chased_fill_reprices_the_whole_ladder():
    """The 22-Jul trade: filled 43.8% above the zone, keeping the card's stop."""
    s = _store()
    t = s.create_from_signal(_card(), lots=1, entry_premium=289.7, lot_size=65,
                             quick_pct=QUICK, sl_pct=SL_PCT, rr1=RR1, rr2=RR2)
    assert t.stop_loss == round(289.7 * (1 - SL_PCT), 2), t.stop_loss
    assert t.stop_loss > 161.9, "kept the card's stop — the whole bug"
    risk = 289.7 - t.stop_loss
    assert abs(t.target1 - (289.7 + risk * RR1)) < 0.05, t.target1
    # The advertised reward:risk now describes the trade actually held.
    assert abs((t.target1 - 289.7) / risk - RR1) < 0.01
    assert t.trailing_sl == t.stop_loss
    print(f"  ENTRY  -> fill Rs289.70: SL Rs{t.stop_loss} T1 Rs{t.target1} "
          f"(card said Rs161.90/Rs215.15)")


def test_reprice_is_recorded_on_the_trade():
    s = _store()
    t = s.create_from_signal(_card(), lots=1, entry_premium=289.7, lot_size=65,
                             quick_pct=QUICK, sl_pct=SL_PCT, rr1=RR1, rr2=RR2)
    kinds = [e.kind for e in t.events]
    assert "repriced" in kinds, kinds
    assert "entry_outside_zone" in kinds, kinds
    over = [e for e in t.events if e.kind == "entry_outside_zone"][0]
    assert "43.8% above" in over.note, over.note
    print(f"  ENTRY  -> journalled: {over.note}")


def test_in_zone_fill_is_not_flagged_as_chasing():
    s = _store()
    t = s.create_from_signal(_card(), lots=1, entry_premium=198.24, lot_size=65,
                             quick_pct=QUICK, sl_pct=SL_PCT, rr1=RR1, rr2=RR2)
    assert "entry_outside_zone" not in [e.kind for e in t.events]
    print("  ENTRY  -> a fill inside the zone carries no warning")


def test_quick_target_never_lands_above_target1():
    """The invariant that broke twice on 22-Jul, on both anchoring paths."""
    for kwargs in (
        dict(sl_pct=SL_PCT, rr1=RR1, rr2=RR2),   # re-priced ladder
        dict(),                                  # legacy: card levels kept
    ):
        s = _store(f"/tmp/tw-entry-inv-{len(kwargs)}.json")
        # A stop 2% away makes 1.5R smaller than the +12% early target, which is
        # exactly the geometry that inverted the two.
        t = s.create_from_signal(_card(premium_sl=193.5, target1=203.0), lots=1,
                                 entry_premium=197.45, lot_size=65,
                                 quick_pct=QUICK, **kwargs)
        assert t.quick_target is None or t.quick_target < t.target1, (
            t.quick_target, t.target1, kwargs)
    print("  ENTRY  -> quick_target dropped when it is not earlier than T1")


def test_legacy_callers_still_get_the_card_levels():
    """Without the mode's multipliers there is nothing to re-price from."""
    s = _store()
    t = s.create_from_signal(_card(), lots=1, entry_premium=289.7, lot_size=65)
    assert (t.stop_loss, t.target1, t.target2) == (161.9, 215.15, 286.1)
    print("  ENTRY  -> no ladder params: card levels preserved, as before")


# --- size guard ---------------------------------------------------------------

def _guard(lots, ack=False, card=None, entry=289.7, disaster_pct=None):
    body = EnterRequest(symbol="NIFTY", lots=lots, acknowledge_oversize=ack)
    _guard_size(body, card or _card(), entry, 65, SL_PCT, RR1, RR2, disaster_pct, _cfg())


def test_size_within_suggestion_passes():
    _guard(lots=3)
    print("  SIZE   -> 3 lots against a suggested 3: no friction")


def test_oversize_is_refused_once_with_the_rupee_risk():
    try:
        _guard(lots=10)
    except HTTPException as exc:
        assert exc.status_code == 409
        d = exc.detail
        assert d["code"] == "oversize_lots", d
        # 10 lots x 65 x (289.70 - 237.55) = Rs 33,897, 68% of the 50k limit.
        assert d["risk_rupees"] == 33897.5, d
        assert "33,897" in d["message"], d
        assert "68% of your ₹50,000 daily loss limit" in d["message"], d
        assert "suggested 3" in d["message"], d
        assert (d["lots"], d["suggested_lots"]) == (10, 3), d
        print(f"  SIZE   -> refused: {d['message']}")
        return
    raise AssertionError("10 lots against a suggested 3 was not challenged")


def test_acknowledged_oversize_is_allowed_through():
    """Advisory-only: /trades/enter records a fill that ALREADY happened, so a
    permanent block would just produce an untracked position — strictly worse."""
    _guard(lots=10, ack=True)
    print("  SIZE   -> acknowledged oversize journals normally")


def test_guard_is_silent_when_the_card_suggests_nothing():
    _guard(lots=10, card=_card(suggested_lots=None))
    print("  SIZE   -> no suggestion on the card: nothing to compare against")


def test_risk_is_quoted_against_the_repriced_stop():
    """Quoting the card's stop would understate a chased fill's risk."""
    try:
        _guard(lots=10)
    except HTTPException as exc:
        card_stop_risk = (289.7 - 161.9) * 10 * 65     # Rs 83,070 — the wrong number
        assert f"{card_stop_risk:,.0f}" not in exc.detail["message"], exc.detail
        assert "₹237.55" in exc.detail["message"], exc.detail
        print("  SIZE   -> risk quoted against the stop the trade will carry")


def test_risk_is_quoted_against_the_disaster_stop_when_it_governs():
    """With the underlying stop primary, the premium stop is only a backstop —
    the trade actually ends at the disaster level, and quoting the narrower
    stop understates the acknowledged rupees ~2.5x. The dialog must show the
    number the journal will really carry."""
    try:
        _guard(lots=10, disaster_pct=0.45)
    except HTTPException as exc:
        d = exc.detail
        # Stop = 289.70 x 0.55 = ₹159.35 → risk (289.70-159.35) x 650 = ₹84,727.50
        assert d["risk_rupees"] == 84727.5, d
        assert "₹159.35" in d["message"], d
        print(f"  SIZE   -> disaster stop governs the quote: {d['risk_rupees']:,.0f}")
        return
    raise AssertionError("oversize with disaster stop was not challenged")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nAll entry-geometry tests passed.")
