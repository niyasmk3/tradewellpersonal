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


# --- protective stop-loss hand-off -------------------------------------------
# This endpoint emits a SELL order. Every number below becomes a real resting
# order against a real position, so each is hand-checked, not recorded.

def _trade(direction=Direction.PE, token=1002, strike=24200.0, status="entered",
           qty=130, trailing_sl=75.4, ltp=94.0, expiry="2026-07-21",
           mode=TradingMode.INTRADAY, entered_at=None, product=None):
    return type("T", (), {
        "id": "T-1", "symbol": "NIFTY", "mode": mode, "direction": direction,
        "contract": f"NIFTY {int(strike)} {direction.value}", "strike": strike,
        "token": token, "expiry": expiry, "quantity": qty,
        "status": type("S", (), {"value": status})(),
        "stop_loss": 75.4, "trailing_sl": trailing_sl, "current_premium": ltp,
        "entered_at": NOW if entered_at is None else entered_at,
        "product": product,
    })()


def _install_trade(trade):
    kb.feed.chain_builder = _FakeBuilder(_universes())
    kb.trade_store.get = lambda tid: trade if tid == trade.id else None  # type: ignore


def _protect(trade):
    _install_trade(trade)
    payload = kb.protect(trade_id="T-1")
    return json.loads(payload.data)[0], payload


def test_protect_builds_a_sell_sl():
    o, p = _protect(_trade())
    assert o["transaction_type"] == "SELL", o["transaction_type"]
    # SL-M is blocked for index options (NSE, Sept 2021) -> must be SL with a price.
    assert o["order_type"] == "SL", o["order_type"]
    assert "trigger_price" in o and "price" in o
    assert o["tradingsymbol"] == "NIFTY2672124200PE", o["tradingsymbol"]
    assert o["quantity"] == 130, o["quantity"]
    assert o["product"] == "MIS", o["product"]
    print(f"  PROTECT-> SELL {o['quantity']} {o['tradingsymbol']} SL trg {o['trigger_price']} lim {o['price']}")


def test_protect_trigger_and_limit_arithmetic():
    """trailing_sl 75.4 -> trigger floors to 75.40; limit = 75.40 x 0.95 = 71.63,
    floored to the 0.05 tick = 71.60. Limit must sit BELOW the trigger."""
    o, _ = _protect(_trade(trailing_sl=75.4))
    assert o["trigger_price"] == 75.4, o["trigger_price"]
    assert o["price"] == 71.6, o["price"]
    assert o["price"] < o["trigger_price"]
    print(f"  PROTECT-> trigger 75.40 -> limit 71.60 (5% below, tick-aligned)")


def test_protect_trigger_rounds_down_never_tightens():
    """An unaligned stop must round DOWN: rounding up would move the stop closer
    than the plan set it and exit early."""
    o, _ = _protect(_trade(trailing_sl=75.44))
    assert o["trigger_price"] == 75.4, o["trigger_price"]
    o2, _ = _protect(_trade(trailing_sl=75.49))
    assert o2["trigger_price"] == 75.45, o2["trigger_price"]
    print("  PROTECT-> trigger floors to the tick (never tightens the stop)")


def test_protect_uses_trailed_stop_not_original():
    o, _ = _protect(_trade(trailing_sl=95.0, ltp=120.0))
    assert o["trigger_price"] == 95.0, o["trigger_price"]
    print("  PROTECT-> honours the TRAILED stop after Target 1")


def test_protect_refuses_closed_position():
    """The naked-short guard: a SELL with no long behind it opens a short."""
    for st in ("exited", "ignored"):
        _install_trade(_trade(status=st))
        try:
            kb.protect(trade_id="T-1")
            assert False, f"must refuse status={st}"
        except HTTPException as e:
            assert e.status_code == 409 and "SHORT" in e.detail, e.detail
    print("  PROTECT-> refuses closed positions (would open a SHORT)")


def test_protect_refuses_when_stop_already_breached():
    """Kite rejects a sell stop whose trigger is above LTP; that state means
    'exit now', not 'rest an order'."""
    _install_trade(_trade(trailing_sl=95.0, ltp=90.0))
    try:
        kb.protect(trade_id="T-1")
        assert False, "must refuse a trigger above LTP"
    except HTTPException as e:
        assert e.status_code == 409 and "exit-now" in e.detail, e.detail
    print("  PROTECT-> refuses trigger above LTP (exit-now situation)")


def test_protect_positional_uses_nrml_and_monthly():
    o, _ = _protect(_trade(token=2002, expiry="2026-07-28", mode=TradingMode.POSITIONAL))
    assert o["product"] == "NRML", o["product"]
    assert o["tradingsymbol"] == "NIFTY2672824200PE", o["tradingsymbol"]
    print("  PROTECT-> positional -> NRML on the MONTHLY contract")


def test_protect_ce_selects_call_leg():
    o, _ = _protect(_trade(direction=Direction.CE, token=1001))
    assert o["tradingsymbol"].endswith("CE"), o["tradingsymbol"]
    assert o["transaction_type"] == "SELL"
    print("  PROTECT-> CE position sells the CALL leg")


def test_protect_expiry_mismatch_refused():
    _install_trade(_trade(expiry="2026-07-28", token=1002))   # weekly token, monthly expiry
    try:
        kb.protect(trade_id="T-1")
        assert False, "expiry mismatch must be refused"
    except HTTPException as e:
        assert e.status_code == 409, e.detail
    print("  PROTECT-> expiry mismatch refused")


def test_protect_carries_a_warning():
    _, p = _protect(_trade())
    assert p.warning and "SHORT" in p.warning, p.warning
    print("  PROTECT-> payload carries the short-position warning")


def test_protect_refuses_near_zero_premium():
    """At a Rs 0.05 stop the 5% buffer cannot be expressed in ticks, so the limit
    would clamp up to equal the trigger. Refuse rather than emit that."""
    _install_trade(_trade(trailing_sl=0.05, ltp=1.0))
    try:
        kb.protect(trade_id="T-1")
        assert False, "must refuse a near-zero stop"
    except HTTPException as e:
        assert e.status_code == 409 and "too small" in e.detail, e.detail
    print("  PROTECT-> refuses near-zero stop (limit could not sit below trigger)")


def test_protect_second_handoff_escalates_warning():
    """Two SELL stops against one long: the second would open a SHORT."""
    seen = {"n": 0}
    def fake_note(tid, trigger):
        prior = seen["n"]
        seen["n"] += 1
        return prior
    kb.trade_store.note_stop_handoff = fake_note  # type: ignore
    _install_trade(_trade())
    p1 = kb.protect(trade_id="T-1")
    assert "ALREADY" not in (p1.warning or ""), p1.warning
    p2 = kb.protect(trade_id="T-1")
    assert "ALREADY" in (p2.warning or "") and "SHORT" in (p2.warning or ""), p2.warning
    print("  PROTECT-> repeat hand-off escalates to a double-sell warning")


def test_protect_prices_stay_tick_aligned_across_range():
    """Kite rejects any price that is not an exact multiple of Rs 0.05."""
    for stop in (2.5, 7.35, 19.25, 75.4, 103.0, 247.85, 1234.55):
        _install_trade(_trade(trailing_sl=stop, ltp=stop * 2))
        o = json.loads(kb.protect(trade_id="T-1").data)[0]
        for field in ("trigger_price", "price"):
            ticks = o[field] / 0.05
            assert abs(round(ticks) - ticks) < 1e-9, f"{field}={o[field]} off-tick at stop {stop}"
        assert o["price"] < o["trigger_price"], (stop, o)
    print("  PROTECT-> trigger+limit tick-aligned from Rs2.50 to Rs1234.55")


def test_protect_refuses_stale_intraday_position():
    """Zerodha auto-squares MIS at ~15:20 IST, so yesterday's intraday row
    describes a position the broker already closed. Selling would go SHORT."""
    _install_trade(_trade(entered_at=NOW - 86400))
    try:
        kb.protect(trade_id="T-1")
        assert False, "stale intraday position must be refused"
    except HTTPException as e:
        assert e.status_code == 409 and "stale" in e.detail, e.detail
    print("  PROTECT-> refuses yesterday's intraday row (MIS already squared off)")


def test_protect_allows_stale_positional_position():
    """Positional (NRML) genuinely carries overnight."""
    o, _ = _protect(_trade(entered_at=NOW - 86400, token=2002, expiry="2026-07-28",
                           mode=TradingMode.POSITIONAL))
    assert o["product"] == "NRML"
    print("  PROTECT-> positional row still protectable the next day")


def test_protect_uses_recorded_product_not_the_mode():
    """An intraday signal filled as NRML must get an NRML stop: MIS and NRML are
    separate books, so the wrong one opens a new short leg."""
    o, _ = _protect(_trade(product="NRML", mode=TradingMode.INTRADAY))
    assert o["product"] == "NRML", o["product"]
    print("  PROTECT-> honours the RECORDED product over the mode default")


def test_protect_legacy_row_warns_about_guessed_product():
    _, p = _protect(_trade(product=None))
    assert "built as MIS" in (p.warning or ""), p.warning
    print("  PROTECT-> legacy row warns the product was inferred")


def test_snap_is_immune_to_float_representation():
    """floor(x/0.05) is one tick low for ~35% of exact multiples (71.60/0.05 ==
    1431.9999999999998). Every exact tick must snap to itself."""
    wrong = [round(i * 0.05, 2) for i in range(1, 4001)
             if kb._snap(round(i * 0.05, 2), up=False) != round(i * 0.05, 2)]
    assert not wrong, f"{len(wrong)} exact ticks snapped away, e.g. {wrong[:5]}"
    assert kb._snap(75.44, up=False) == 75.4
    assert kb._snap(75.41, up=True) == 75.45
    print("  SNAP   -> all 4000 exact ticks stable; 75.44->75.40 down, 75.41->75.45 up")


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
