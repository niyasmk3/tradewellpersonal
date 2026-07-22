"""Signal re-price tests.

A refresh re-prices an active card against the current premium so a stale
positional signal becomes orderable. It changes what a later Kite hand-off
sends, so it must: use the same ladder maths as issue time, keep the trade's
identity, re-stamp freshness, and never touch the daily signal budget.

Run:  python backend/tests/test_reprice.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.signals import risk as risk_mod
from app.signals.models import (
    Action, Bias, Direction, MarketStatus, Regime, ScoreBreakdown,
    SignalCard, SignalResponse, SignalState, TradingMode,
)
from app.signals.store import SignalStore, ThrottleConfig

BASE = int(time.time()) // 86400 * 86400 + 5 * 3600


def _card(created, valid_span=6 * 3600, cid="C"):
    return SignalCard(
        id=f"{cid}-{created}", symbol="NIFTY", mode=TradingMode.POSITIONAL, title="t",
        action=Action.BUY_PE, direction=Direction.PE, state=SignalState.ACTIVE,
        contract="NIFTY 24150 PE", strike=24150.0, token=999, expiry="2026-07-28",
        entry_low=26.85, entry_high=27.65, premium_sl=22.20, disaster_sl=14.90,
        quick_target=30.35, target1=34.45, target2=39.35, trailing_sl_rule="r",
        risk_reward=1.5, confidence=80.0, underlying_invalidation="NIFTY below 24210",
        invalidation_note="n", invalidation_level=24210.4, invalidation_dir="below",
        created_at=created, valid_until=created + valid_span, ref_entry_premium=27.10,
        score=ScoreBreakdown(direction=Direction.PE, components=[], total=80.0, max=100),
    )


def _resp(card):
    return SignalResponse(
        symbol="NIFTY", mode=TradingMode.POSITIONAL, evaluated_at=card.created_at,
        status=MarketStatus(
            symbol="NIFTY", mode=TradingMode.POSITIONAL, regime=Regime.MODERATE_BEARISH,
            regime_label="R", bias=Bias.BEARISH, bull_score=20, bear_score=80,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=Action.BUY_PE, signal=card, no_trade_reason=None, score=None,
    )


def _store_with_active(card):
    s = SignalStore(store_path=None)
    s.reconcile(_resp(card), card.created_at,
                ThrottleConfig(min_gap_s=0, cooldown_s=0, flip_guard_s=0))
    return s


def test_reprice_recomputes_the_ladder_from_the_new_premium():
    """The whole point: entry zone and levels track the current premium."""
    old = _card(created=BASE)
    old_entry_high = old.entry_high                    # snapshot: the store mutates in place
    s = _store_with_active(old)
    new_ltp = 31.50                                    # premium ran up since issue
    ladder = risk_mod.price_ladder(new_ltp, 0.18, 2.0, 3.5, disaster_pct=0.45, quick_pct=0.12)
    now = BASE + 2 * 3600
    out = s.reprice_active("NIFTY", TradingMode.POSITIONAL, new_ltp, ladder, now)

    assert out is not None
    # Levels must equal the ladder computed from 31.50, NOT the old 27.10 values.
    assert out.entry_high == ladder["entry_high"] == round(round(31.50 * 1.02 / 0.05) * 0.05, 2)
    assert out.premium_sl == ladder["premium_sl"]
    assert out.target1 == ladder["target1"]
    assert out.ref_entry_premium == 31.50
    assert out.entry_high > old_entry_high             # tracked the premium up
    print(f"  REPRICE-> zone re-priced 27.10 -> 31.50: entry_high {old_entry_high} -> {out.entry_high}")


def test_reprice_restamps_freshness_but_keeps_identity():
    old = _card(created=BASE, cid="KEEP")
    s = _store_with_active(old)
    now = BASE + 3 * 3600
    span = old.valid_until - old.created_at
    ladder = risk_mod.price_ladder(30.0, 0.18, 2.0, 3.5)
    out = s.reprice_active("NIFTY", TradingMode.POSITIONAL, 30.0, ladder, now)

    assert out.id == old.id                             # same trade
    assert out.strike == old.strike and out.direction == old.direction
    assert out.invalidation_level == old.invalidation_level  # index level untouched
    assert out.created_at == now                        # re-stamped
    assert out.valid_until == now + span                # window from now
    print(f"  REPRICE-> same id/strike/invalidation; created_at re-stamped to now")


def test_reprice_does_not_consume_the_daily_signal_budget():
    """Refreshing a card you're already looking at is not a new signal."""
    old = _card(created=BASE)
    s = _store_with_active(old)
    slot_before = s._slots[f"NIFTY:{TradingMode.POSITIONAL.value}"].issued
    ladder = risk_mod.price_ladder(30.0, 0.18, 2.0, 3.5)
    s.reprice_active("NIFTY", TradingMode.POSITIONAL, 30.0, ladder, BASE + 1000)
    slot_after = s._slots[f"NIFTY:{TradingMode.POSITIONAL.value}"].issued
    assert slot_before == slot_after == 1, (slot_before, slot_after)
    print("  REPRICE-> daily signal count unchanged by a refresh")


def test_reprice_no_active_card_returns_none():
    s = SignalStore(store_path=None)
    ladder = risk_mod.price_ladder(30.0, 0.18, 2.0, 3.5)
    assert s.reprice_active("NIFTY", TradingMode.POSITIONAL, 30.0, ladder, BASE) is None
    print("  REPRICE-> no active card -> None (route turns this into a 409)")


def test_reprice_latest_reflects_the_change():
    """The GET /signals view must show the refreshed levels immediately."""
    old = _card(created=BASE)
    s = _store_with_active(old)
    ladder = risk_mod.price_ladder(31.50, 0.18, 2.0, 3.5)
    now = BASE + 7200
    s.reprice_active("NIFTY", TradingMode.POSITIONAL, 31.50, ladder, now)
    latest = s.latest("NIFTY", TradingMode.POSITIONAL)
    assert latest.signal.entry_high == ladder["entry_high"]
    assert latest.evaluated_at == now
    print("  REPRICE-> latest() serves the refreshed card and a fresh evaluated_at")


def test_ladder_matches_engine_build_exactly():
    """price_ladder is the same maths build() uses — a drift here would make a
    refreshed card price differently from a freshly issued one."""
    import pandas as pd
    df = pd.DataFrame({"open": [24000.0] * 3, "high": [24010.0] * 3,
                       "low": [23990.0] * 3, "close": [24000.0] * 3})
    ind = type("I", (), {"vwap": None})()
    plan = risk_mod.build(Direction.PE, 27.10, df, ind, 24000.0, "NIFTY", "3m",
                          0.18, 2.0, 3.5, disaster_pct=0.45, quick_pct=0.12)
    ladder = risk_mod.price_ladder(27.10, 0.18, 2.0, 3.5, disaster_pct=0.45, quick_pct=0.12)
    for k in ("entry_low", "entry_high", "premium_sl", "disaster_sl", "quick_target",
              "target1", "target2"):
        assert getattr(plan, k) == ladder[k], (k, getattr(plan, k), ladder[k])
    print("  LADDER -> refresh ladder == engine build, field for field")


def test_close_active_cancels_and_blanks_the_response():
    """The score-revalidation path: a card whose thesis died is CLOSED, not
    re-priced. The served response must then show no signal."""
    old = _card(created=BASE)
    s = _store_with_active(old)
    ok = s.close_active("NIFTY", TradingMode.POSITIONAL, BASE + 3600,
                        "PE score is now 50, below 72 — setup no longer valid, signal closed")
    assert ok is True
    latest = s.latest("NIFTY", TradingMode.POSITIONAL)
    assert latest.signal is None
    assert latest.action.value == "avoid"
    assert "no longer valid" in (latest.no_trade_reason or "")
    print("  CLOSE  -> active card cancelled, response blanked with the reason")


def test_close_active_no_card_returns_false():
    s = SignalStore(store_path=None)
    assert s.close_active("NIFTY", TradingMode.POSITIONAL, BASE, "x") is False
    print("  CLOSE  -> nothing to close -> False")


def test_a_closed_card_is_not_repriceable():
    """After close, reprice_active must find nothing (the route 409s / re-checks)."""
    old = _card(created=BASE)
    s = _store_with_active(old)
    s.close_active("NIFTY", TradingMode.POSITIONAL, BASE + 60, "dead")
    ladder = risk_mod.price_ladder(30.0, 0.18, 2.0, 3.5)
    assert s.reprice_active("NIFTY", TradingMode.POSITIONAL, 30.0, ladder, BASE + 120) is None
    print("  CLOSE  -> a closed card cannot then be re-priced")


# --- endpoint-level: the revalidation branch itself ---------------------------

def _install_endpoint(resp, ltp=30.0):
    """Point the reprice route at a synthetic store state + a live premium."""
    from app.api import routes_signals as rs
    from app.signals import store as store_mod
    from app import state as state_mod

    class _FakeStore:
        def __init__(self): self.closed = None; self.repriced = None
        def latest(self, sym, mode): return resp
        def close_active(self, sym, mode, now, reason): self.closed = reason; return True
        def reprice_active(self, sym, mode, ltp, ladder, now):
            self.repriced = ltp; return resp.signal
    fake = _FakeStore()
    store_mod.signal_store = fake
    state_mod.market_state.ticks = {999: {"last_price": ltp}} if ltp else {}
    return rs, fake


def _status(regime, bull, bear):
    return MarketStatus(
        symbol="NIFTY", mode=TradingMode.POSITIONAL, regime=regime, regime_label="R",
        bias=Bias.BEARISH, bull_score=bull, bear_score=bear, headline="h",
        vix_status=None, news_label=None, news_net=None, notes=[],
    )


def test_endpoint_closes_when_live_score_below_threshold():
    """The user's case: a PE card held at issue-confidence 80 whose LIVE bear
    score has since fallen to 50 (needs 72 positional) is CLOSED, not re-priced."""
    card = _card(created=BASE)
    resp = SignalResponse(symbol="NIFTY", mode=TradingMode.POSITIONAL, evaluated_at=BASE,
                          status=_status(Regime.MODERATE_BEARISH, 20.0, 50.0),
                          action=Action.BUY_PE, signal=card, no_trade_reason=None, score=None)
    rs, fake = _install_endpoint(resp)
    out = rs.reprice_signal("NIFTY", "positional")
    assert out.status == "closed", out.status
    assert out.score == 50.0 and out.score_needed == 72
    assert fake.closed and fake.repriced is None       # closed, never re-priced
    print(f"  ENDPOINT-> bear 50 < 72 -> CLOSED (not re-priced)")


def test_endpoint_reprices_when_live_score_still_valid():
    card = _card(created=BASE)
    resp = SignalResponse(symbol="NIFTY", mode=TradingMode.POSITIONAL, evaluated_at=BASE,
                          status=_status(Regime.MODERATE_BEARISH, 20.0, 80.0),
                          action=Action.BUY_PE, signal=card, no_trade_reason=None, score=None)
    rs, fake = _install_endpoint(resp, ltp=31.5)
    out = rs.reprice_signal("NIFTY", "positional")
    assert out.status == "repriced" and out.signal is not None
    assert fake.repriced == 31.5 and fake.closed is None
    print("  ENDPOINT-> bear 80 >= 72 -> RE-PRICED against live 31.5")


def test_endpoint_warmup_neither_closes_nor_reprices():
    """A warmup-depressed score is 'no data', not 'thesis dead' — must not close."""
    from fastapi import HTTPException
    card = _card(created=BASE)
    resp = SignalResponse(symbol="NIFTY", mode=TradingMode.POSITIONAL, evaluated_at=BASE,
                          status=_status(Regime.WARMING_UP, 25.0, 40.0),
                          action=Action.BUY_PE, signal=card, no_trade_reason=None, score=None)
    rs, fake = _install_endpoint(resp)
    try:
        rs.reprice_signal("NIFTY", "positional")
        assert False, "warmup should 409"
    except HTTPException as e:
        assert e.status_code == 409 and "warming up" in e.detail.lower()
    assert fake.closed is None and fake.repriced is None
    print("  ENDPOINT-> warming up -> 409, card untouched")


def test_endpoint_ce_uses_bull_score():
    """Direction→score mapping: a CE card is validated against bull_score."""
    card = _card(created=BASE)
    card.direction = Direction.CE
    resp = SignalResponse(symbol="NIFTY", mode=TradingMode.POSITIONAL, evaluated_at=BASE,
                          status=_status(Regime.MODERATE_BULLISH, 45.0, 90.0),  # bull LOW, bear high
                          action=Action.BUY_CE, signal=card, no_trade_reason=None, score=None)
    rs, fake = _install_endpoint(resp)
    out = rs.reprice_signal("NIFTY", "positional")
    # bull 45 < 72 -> closed, even though bear is 90. Must read the RIGHT score.
    assert out.status == "closed" and out.score == 45.0, (out.status, out.score)
    print("  ENDPOINT-> CE validated against bull_score (45<72 -> closed), not bear")


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
