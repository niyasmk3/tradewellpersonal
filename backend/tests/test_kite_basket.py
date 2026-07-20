"""Kite Publisher hand-off tests.

This is the one place Tradewell composes something a broker will act on, so
these tests call the REAL route and assert on the payload it actually emits.
(An earlier version asserted a hand-written dict and would have passed while a
critical wrong-expiry bug was live — see test_positional_card_never_resolves_weekly.)

Run:  python backend/tests/test_kite_basket.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import time
from datetime import date

from fastapi import HTTPException

from app.api import routes_kite_basket as kb
from app.kite.instruments import OptionUniverse, StrikePair
from app.signals.models import (
    Action,
    Direction,
    ScoreBreakdown,
    SignalCard,
    SignalState,
    TradingMode,
)

NOW = int(time.time())


class _FakeBuilder:
    def __init__(self, universes):
        self.universes = universes


def _pair(strike, ce_tok, pe_tok, ce_sym, pe_sym, lot=65):
    return StrikePair(
        strike=strike, ce_token=ce_tok, pe_token=pe_tok,
        ce_symbol=ce_sym, pe_symbol=pe_sym, ce_lot_size=lot, pe_lot_size=lot,
    )


def _universes():
    """Weekly + monthly for NIFTY (same strikes, different expiry/tokens) plus a
    FINNIFTY ladder that OVERLAPS NIFTY's strikes — the real-world trap."""
    weekly = OptionUniverse(symbol="NIFTY", expiry=date(2026, 7, 21), step=50)
    weekly.strikes[24200.0] = _pair(24200.0, 1001, 1002, "NIFTY2672124200CE", "NIFTY2672124200PE")

    monthly = OptionUniverse(symbol="NIFTY", expiry=date(2026, 7, 28), step=50)
    monthly.strikes[24200.0] = _pair(24200.0, 2001, 2002, "NIFTY2672824200CE", "NIFTY2672824200PE")

    fin = OptionUniverse(symbol="FINNIFTY", expiry=date(2026, 7, 21), step=50)
    fin.strikes[24200.0] = _pair(24200.0, 3001, 3002, "FINNIFTY2672124200CE", "FINNIFTY2672124200PE", lot=25)

    return {
        "NIFTY:nearest": weekly,     # inserted FIRST on purpose (matches production order)
        "NIFTY:monthly": monthly,
        "FINNIFTY:nearest": fin,
        "FINNIFTY:monthly": fin,
    }


def _card(direction=Direction.PE, strike=24200.0, token=1002, expiry="2026-07-21",
          mode=TradingMode.INTRADAY, symbol="NIFTY"):
    return SignalCard(
        id="S-1", symbol=symbol, mode=mode, title="t", action=Action.BUY_PE,
        direction=direction, state=SignalState.ACTIVE,
        contract=f"{symbol} {int(strike)} {direction.value}", strike=strike,
        token=token, expiry=expiry,
        entry_low=90.0, entry_high=94.0, premium_sl=75.4, target1=116.9, target2=133.5,
        trailing_sl_rule="r", risk_reward=1.5, confidence=74.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=NOW, valid_until=NOW + 480,
        score=ScoreBreakdown(direction=direction, components=[], total=74.0, max=100),
    )


def _install(card, mode=TradingMode.INTRADAY, symbol="NIFTY", lot_size=65):
    """Point the route at a synthetic signal + universe.

    The store is stubbed with a minimal object exposing `.signal` — building a
    full SignalResponse would need a MarketStatus this test doesn't care about.
    """
    kb.feed.chain_builder = _FakeBuilder(_universes())
    kb.signal_store.latest = lambda s, m: type("R", (), {"signal": card})()  # type: ignore
    kb.market_state.underlyings = {symbol.upper(): type("M", (), {"lot_size": lot_size})()}  # type: ignore


def _order(**kw):
    payload = kb.basket(**kw)
    return json.loads(payload.data)[0], payload


def test_intraday_weekly_resolves_weekly():
    _install(_card())
    o, p = _order(symbol="NIFTY", mode=TradingMode.INTRADAY, lots=2, signal_id="S-1")
    assert o["tradingsymbol"] == "NIFTY2672124200PE", o["tradingsymbol"]
    assert o["quantity"] == 2 * 65 == p.quantity
    assert o["transaction_type"] == "BUY" and o["order_type"] == "LIMIT"
    assert o["price"] == 94.0 and o["readonly"] is False
    assert o["product"] == "MIS" and o["exchange"] == "NFO"
    print("  INTRADAY -> weekly PE, MIS, BUY/LIMIT, qty 130")


def test_kite_input_constraints():
    """Constraints the live API enforces but the v3 docs understate.
    Both were found by Kite REJECTING a real basket, not by the docs."""
    _install(_card())
    o, _ = _order(symbol="NIFTY", mode=TradingMode.INTRADAY, lots=1, signal_id="S-1")
    # `tag` max 8 chars — docs claim 20; "tradewell" (9) was rejected.
    assert len(o["tag"]) <= 8, f"tag too long for Kite: {o['tag']!r}"
    # LIMIT price must sit on the ₹0.05 NSE tick.
    cents = round(o["price"] * 100)
    assert cents % 5 == 0, f"price {o['price']} is not on a 0.05 tick"
    print(f"  KITE-LIMITS -> tag {o['tag']!r} (<=8), price {o['price']} on 0.05 tick")


def test_price_snaps_up_to_tick():
    """An entry_high off-tick must round UP, never below the entry zone."""
    card = _card()
    card.entry_high = 94.03
    _install(card)
    o, _ = _order(symbol="NIFTY", mode=TradingMode.INTRADAY, lots=1, signal_id="S-1")
    assert o["price"] == 94.05, o["price"]
    assert o["price"] >= card.entry_high
    print("  TICK -> 94.03 snapped up to 94.05")


def test_positional_card_never_resolves_weekly():
    """REGRESSION: the resolver used to scan all universes and strike-match
    inside NIFTY:nearest first, handing a monthly card the WEEKLY contract."""
    card = _card(token=2002, expiry="2026-07-28", mode=TradingMode.POSITIONAL)
    _install(card, mode=TradingMode.POSITIONAL)
    o, _ = _order(symbol="NIFTY", mode=TradingMode.POSITIONAL, lots=1, signal_id="S-1")
    assert o["tradingsymbol"] == "NIFTY2672824200PE", o["tradingsymbol"]
    assert "26721" not in o["tradingsymbol"], "resolved the WEEKLY contract for a monthly card"
    assert o["product"] == "NRML"
    print("  POSITIONAL -> monthly PE (not weekly), NRML")


def test_strike_fallback_stays_in_its_own_universe():
    """Even with no token, a monthly card must not fall back to the weekly."""
    card = _card(token=None, expiry="2026-07-28", mode=TradingMode.POSITIONAL)
    _install(card, mode=TradingMode.POSITIONAL)
    o, _ = _order(symbol="NIFTY", mode=TradingMode.POSITIONAL, lots=1, signal_id="S-1")
    assert o["tradingsymbol"] == "NIFTY2672824200PE", o["tradingsymbol"]
    print("  NO-TOKEN -> strike fallback confined to the card's universe")


def test_expiry_mismatch_is_refused():
    """Universe rolled since the card was created -> refuse, don't substitute."""
    card = _card(token=None, expiry="2026-07-14")   # a dead expiry
    _install(card)
    try:
        _order(symbol="NIFTY", mode=TradingMode.INTRADAY, lots=1, signal_id="S-1")
    except HTTPException as e:
        assert e.status_code == 409 and "expiry changed" in e.detail
        print("  ROLLED -> 409 instead of a different expiry")
        return
    raise AssertionError("expected a 409 on expiry mismatch")


def test_direction_selects_the_right_leg():
    _install(_card(direction=Direction.CE, token=1001))
    o, _ = _order(symbol="NIFTY", mode=TradingMode.INTRADAY, lots=1, signal_id="S-1")
    assert o["tradingsymbol"].endswith("CE"), o["tradingsymbol"]
    print("  DIRECTION -> CE card yields the CE contract")


def test_option_lot_size_wins_over_stale_future_lot():
    """NSE lot revisions land on new series first; the future's lot can lag."""
    _install(_card(), lot_size=75)          # stale future lot size
    o, _ = _order(symbol="NIFTY", mode=TradingMode.INTRADAY, lots=1, signal_id="S-1")
    assert o["quantity"] == 65, o["quantity"]
    print("  LOTSIZE -> option's 65 used, not the future's stale 75")


def test_stale_and_mismatched_cards_are_refused():
    card = _card()
    card.valid_until = NOW - 1                      # expired
    _install(card)
    for kwargs, why in [
        (dict(symbol="NIFTY", mode=TradingMode.INTRADAY, lots=1, signal_id="S-1"), "expired"),
        (dict(symbol="NIFTY", mode=TradingMode.INTRADAY, lots=1, signal_id="OTHER"), "id mismatch"),
    ]:
        try:
            _order(**kwargs)
            raise AssertionError(f"expected 409 for {why}")
        except HTTPException as e:
            assert e.status_code == 409
    print("  GUARDS -> expired card and signal-id mismatch both 409")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
