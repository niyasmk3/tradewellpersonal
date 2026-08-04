"""History-truth fixes (04-Aug): birth-pinned reprice history, issue-time
serialization, reprice cool-down, and shadow-store archiving.

Run:  python backend/tests/test_history_truth.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import tempfile
import time
from pathlib import Path

from app.signals.archive import SignalArchive
from app.signals.models import (
    Action, Bias, Direction, MarketStatus, Regime, ScoreBreakdown,
    SignalCard, SignalResponse, SignalState, TradingMode,
)
from app.signals.store import SignalStore, ThrottleConfig

NOW = int(time.time())


def _card(cid="H", at=NOW, entry=14.3):
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_PE, direction=Direction.PE, state=SignalState.ACTIVE,
        contract="NIFTY 24500 PE", strike=24500.0, token=777, expiry="2026-08-07",
        entry_low=round(entry * 0.99, 2), entry_high=round(entry * 1.02, 2),
        premium_sl=round(entry * 0.82, 2), target1=round(entry * 1.27, 2),
        target2=round(entry * 1.45, 2), trailing_sl_rule="r", risk_reward=1.5,
        confidence=82.0, underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480, ref_entry_premium=entry,
        score=ScoreBreakdown(direction=Direction.PE, components=[], total=82.0),
    )


def _resp(card, at=NOW, bear_score=80):
    return SignalResponse(
        symbol="NIFTY", mode=TradingMode.INTRADAY, evaluated_at=at,
        status=MarketStatus(
            symbol="NIFTY", mode=TradingMode.INTRADAY, regime=Regime.MODERATE_BEARISH,
            regime_label="R", bias=Bias.BEARISH, bull_score=30, bear_score=bear_score,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=card.action if card else Action.AVOID,
        signal=card, no_trade_reason=None, score=None,
    )


def _ladder(entry):
    return {"entry_low": round(entry * 0.99, 2), "entry_high": round(entry * 1.02, 2),
            "premium_sl": round(entry * 0.82, 2), "target1": round(entry * 1.27, 2),
            "target2": round(entry * 1.45, 2), "disaster_sl": None,
            "quick_target": round(entry * 1.12, 2),
            "trailing_sl_rule": "r", "risk_reward": 1.5}


def test_birth_ladder_survives_unlimited_reprices():
    """04-Aug: 27 reprices rotated the issue-time ladder out of the 10-slot
    history. Slot 0 is now pinned forever."""
    s = SignalStore(store_path=None)
    s.reconcile(_resp(_card()), NOW, ThrottleConfig(min_gap_s=0, cooldown_s=0))
    for i in range(27):
        s.reprice_active("NIFTY", TradingMode.INTRADAY,
                         14.3 + i, _ladder(14.3 + i), NOW + 60 * (i + 1))
    card = s.latest("NIFTY", TradingMode.INTRADAY).signal
    rh = card.reprice_history
    assert len(rh) <= 10, len(rh)
    # Slot 0 must still be the BIRTH ladder (entry ~14.3 zone low ~14.16).
    assert abs(rh[0]["entry_low"] - round(14.3 * 0.99, 2)) < 0.01, rh[0]
    assert card.entry_low != rh[0]["entry_low"], "final ladder must differ"
    # The TRUE count survives the 10-slot rotation: 27 means 27, not 10.
    from app.api.routes_signals import _issue_view
    assert card.reprice_total == 27, card.reprice_total
    assert _issue_view(card)["reprice_count"] == 27
    print("  PIN    -> birth ladder survives 27 reprices at slot 0; count stays 27")


def test_issue_view_serialization():
    from app.api.routes_signals import _issue_view

    s = SignalStore(store_path=None)
    s.reconcile(_resp(_card()), NOW, ThrottleConfig(min_gap_s=0, cooldown_s=0))
    fresh = s.latest("NIFTY", TradingMode.INTRADAY).signal
    v0 = _issue_view(fresh)
    assert v0["repriced_at"] is None and "issued_entry_low" not in v0

    s.reprice_active("NIFTY", TradingMode.INTRADAY, 30.9, _ladder(30.9), NOW + 300)
    rp = s.latest("NIFTY", TradingMode.INTRADAY).signal
    v1 = _issue_view(rp)
    assert v1["repriced_at"] == NOW + 300
    assert abs(v1["issued_entry_low"] - round(14.3 * 0.99, 2)) < 0.01, v1
    assert v1["reprice_count"] == 1
    print("  VIEW   -> un-repriced rows stay plain; repriced rows carry issued_*")


def test_reprice_cooldown_guard():
    # The route imports signal_store lazily from the store module at call
    # time, so the store MODULE's attribute is the patch point.
    import app.api.routes_signals as rs
    import app.signals.store as store_mod
    from fastapi import HTTPException

    s = SignalStore(store_path=None)
    s.reconcile(_resp(_card()), NOW, ThrottleConfig(min_gap_s=0, cooldown_s=0))
    s.reprice_active("NIFTY", TradingMode.INTRADAY, 15.0, _ladder(15.0), int(time.time()))
    orig = store_mod.signal_store
    store_mod.signal_store = s
    try:
        try:
            rs.reprice_signal("NIFTY", mode="intraday")
            raise AssertionError("expected 429 cool-down")
        except HTTPException as e:
            assert e.status_code == 429 and "Re-priced" in str(e.detail), e.detail
    finally:
        store_mod.signal_store = orig
    print("  COOL   -> second refresh within 30s refused with a reason")


def test_cooldown_never_blocks_dead_thesis_close():
    """Review catch: the cool-down must be checked AFTER the decay-close
    branch — a card whose score collapsed 5s after a reprice still has to
    close on this click, not sit orderable for 30s."""
    import app.api.routes_signals as rs
    import app.signals.store as store_mod

    s = SignalStore(store_path=None)
    # Stored response carries bear_score 70 — below intraday score_valid 72.
    s.reconcile(_resp(_card(), bear_score=70), NOW,
                ThrottleConfig(min_gap_s=0, cooldown_s=0))
    s.reprice_active("NIFTY", TradingMode.INTRADAY, 15.0, _ladder(15.0), int(time.time()))
    orig = store_mod.signal_store
    store_mod.signal_store = s
    try:
        out = rs.reprice_signal("NIFTY", mode="intraday")
        assert out.status == "closed", out
        assert s.latest("NIFTY", TradingMode.INTRADAY).signal is None
    finally:
        store_mod.signal_store = orig
    print("  CLOSE  -> decayed thesis closes even inside the 30s cool-down")


def test_shadow_store_archives_and_merge():
    with tempfile.TemporaryDirectory() as d:
        arch_path = Path(d) / ".shadow_arch.jsonl"
        arch = SignalArchive(arch_path)
        s = SignalStore(store_path=None)
        s.archive = arch
        s.reconcile(_resp(_card("SH")), NOW, ThrottleConfig(min_gap_s=0, cooldown_s=0))
        lines = [json.loads(x) for x in open(arch_path)]
        assert len(lines) == 1 and lines[0]["card"]["id"].startswith("SH-"), lines
        # merge harvest from a shadow-store FILE the archive has not seen.
        store_file = Path(d) / ".hollow_signals.json"
        other = _card("MERGED", at=NOW - 60)
        store_file.write_text(json.dumps(
            {"NIFTY:intraday": {"active": None,
                                "history": [other.model_dump(mode="json")]}}))
        arch2 = SignalArchive(Path(d) / ".shadow_arch.jsonl")
        arch2.merge_store_file(store_names=(".hollow_signals.json",))
        ids = {json.loads(x)["card"]["id"] for x in open(arch_path)}
        assert any(i.startswith("MERGED-") for i in ids), ids
    print("  SHADOW -> shadow adopts archived; boot-merge harvests shadow files")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
