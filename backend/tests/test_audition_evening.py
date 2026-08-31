"""Intraday paper-audition demotion + evening-positional size cut (31-Aug).

August evidence: intraday 14 clean fills, 14% WR, -Rs5,050, zero Target-1
exits; one 15:21 positional entry gapped to -50.8%. These tests pin the two
decisions: the shared paper_only_block refuses intraday (and scalp) on both
order paths until the flags flip, and evening positional suggestions shrink.

Run:  python backend/tests/test_audition_evening.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import Settings
from app.signals.models import TradingMode
from app.signals.modes import paper_only_block


def _cfg(**over):
    return Settings(_env_file=None, **over)


def test_intraday_demoted_by_default():
    cfg = _cfg()
    assert cfg.intraday_live_enabled is False
    block = paper_only_block(TradingMode.INTRADAY, cfg)
    assert block and "paper audition" in block and "INTRADAY_LIVE_ENABLED" in block
    # Scalp's original audition unchanged.
    assert paper_only_block(TradingMode.SCALP, cfg)
    # Positional stays live.
    assert paper_only_block(TradingMode.POSITIONAL, cfg) is None
    print("  BLOCK  -> intraday+scalp refused on order paths; positional live")


def test_flags_rearm_modes():
    cfg = _cfg(INTRADAY_LIVE_ENABLED=True, SCALP_LIVE_ENABLED=True)
    assert paper_only_block(TradingMode.INTRADAY, cfg) is None
    assert paper_only_block(TradingMode.SCALP, cfg) is None
    print("  REARM  -> the flags restore live entry, nothing else needed")


def test_intraday_label_reflects_audition():
    from app.signals.modes import build_profiles
    lbl = build_profiles(_cfg())["intraday"].label
    assert "paper audition" in lbl
    lbl2 = build_profiles(_cfg(INTRADAY_LIVE_ENABLED=True))["intraday"].label
    assert lbl2 == "Intraday"
    print("  LABEL  -> profile label tracks the audition state")


def _sized_card(svc, mode, at_epoch):
    """Run _apply_sizing on a minimal positional/intraday card at a frozen clock."""
    from app.signals.models import (Action, Bias, Direction, MarketStatus,
                                    Regime, ScoreBreakdown, ScoreComponent,
                                    SignalCard, SignalResponse, SignalState)
    card = SignalCard(
        id="S-1", symbol="NIFTY", mode=mode, title="t", action=Action.BUY_CE,
        direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24500 CE", strike=24500.0, expiry="2026-09-29",
        entry_low=99.0, entry_high=101.0, premium_sl=70.0, target1=160.0,
        target2=205.0, trailing_sl_rule="r", risk_reward=2.0, confidence=75.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at_epoch, valid_until=at_epoch + 3600,
        ref_entry_premium=100.0, disaster_sl=55.0,
        score=ScoreBreakdown(direction=Direction.CE, components=[
            ScoreComponent(name="Volume confirmation", points=9.0, max=15)],
            total=75.0),
    )
    resp = SignalResponse(
        symbol="NIFTY", mode=mode, evaluated_at=at_epoch,
        status=MarketStatus(symbol="NIFTY", mode=mode,
                            regime=Regime.STRONG_BULLISH, regime_label="R",
                            bias=Bias.BULLISH, bull_score=75, bear_score=20,
                            headline="h", vix_status=None, news_label=None,
                            news_net=None, notes=[]),
        action=Action.BUY_CE, signal=card, no_trade_reason=None, score=None)
    with mock.patch("time.time", return_value=at_epoch):
        svc._apply_sizing(resp, "NIFTY")
    return card


def _svc(**over):
    from app.signals.service import SignalService

    class _State:
        underlyings = {}
        def limits(self):
            return {}
    svc = SignalService.__new__(SignalService)
    svc.cfg = Settings(_env_file=None, TRADING_CAPITAL=500000,
                       RISK_PER_TRADE_PCT=2.0, **over)
    svc.state = _State()
    return svc


def _ist_epoch(hh, mm):
    import time as t
    day = (int(t.time()) + 19800) // 86400
    return day * 86400 - 19800 + hh * 3600 + mm * 60


def test_evening_positional_size_cut():
    svc = _svc()
    # Morning positional: full suggestion. capital 5L x 2% = 10k budget;
    # risk/unit = 100-55 (disaster operative? stop_primary underlying needs
    # capital>0 -> operative = disaster 55) = 45; lot from meta absent -> the
    # card carries no lot -> sizing returns early. Give the card a token-less
    # lot via underlyings meta instead: simpler - patch _option_lot_size.
    with mock.patch("app.signals.service._option_lot_size", return_value=65):
        am = _sized_card(svc, TradingMode.POSITIONAL, _ist_epoch(10, 30))
        pm = _sized_card(svc, TradingMode.POSITIONAL, _ist_epoch(14, 45))
        intr = _sized_card(svc, TradingMode.INTRADAY, _ist_epoch(14, 45))
    assert am.suggested_lots and am.suggested_lots >= 2, am.sizing_note
    assert pm.suggested_lots == max(1, int(am.suggested_lots * 0.5))
    assert "EVENING" in pm.sizing_note and "EVENING" not in am.sizing_note
    # Intraday untouched by the evening rule (its cutoff already bars 14:15+
    # entries; sizing itself must not double-punish).
    assert "EVENING" not in (intr.sizing_note or "")
    print("  EVENING-> positional suggestion halved at/after 14:30, noted")


def test_evening_factor_off_switch():
    svc = _svc(EVENING_POSITIONAL_SIZE_FACTOR=1.0)
    with mock.patch("app.signals.service._option_lot_size", return_value=65):
        pm = _sized_card(svc, TradingMode.POSITIONAL, _ist_epoch(14, 45))
    assert "EVENING" not in (pm.sizing_note or "")
    print("  OFF    -> factor 1.0 disables the cut cleanly")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
