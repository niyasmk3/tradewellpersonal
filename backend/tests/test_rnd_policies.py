"""R3 policy ledgers + insight scanner: booking, overlays, pairing, gating."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings
from app.rnd import policies as P
from app.rnd import scanner as S
from app.rnd import analytics as A
from app.signals.models import (
    Action,
    Direction,
    ScoreBreakdown,
    SignalCard,
    SignalState,
    TradingMode,
)
from app.trades.store import TradeStore

IST = timezone(timedelta(hours=5, minutes=30))
E = int(datetime(2026, 8, 12, 11, 0, tzinfo=IST).timestamp())


def _settings(**over) -> Settings:
    base = dict(KITE_API_KEY="t", KITE_API_SECRET="t")
    base.update(over)
    return Settings(_env_file=None, **base)


CFG = _settings()


def _card(cid="R", mode=TradingMode.INTRADAY, at=E):
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=mode, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24000 CE", strike=24000.0, expiry="2026-08-13",
        entry_low=99.0, entry_high=101.0, premium_sl=82.0, target1=112.0,
        target2=125.0, trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=[], total=81.0),
        ref_entry_premium=100.0, lot_size=75, suggested_lots=1,
    )


def _store() -> TradeStore:
    return TradeStore(path=Path(tempfile.mkdtemp()) / "p.json")


def _book(store, card, tag=None):
    notes = f"hollow: {tag}: {P.TAGS[tag]} — R&D policy twin" if tag else None
    lots = 2 if tag in ("rnd-plad", "rnd-base2") else 1
    t = store.create_from_signal(card, lots, 100.0, 75, product=None,
                                 disaster_pct=None, quick_pct=0.12,
                                 sl_pct=0.18, rr1=1.5, rr2=2.5, notes=notes)
    return t


def test_shadow_class_recognizes_policy_twins():
    from app.paper.service import shadow_class
    store = _store()
    tw = _book(store, _card(), "rnd-p5w")
    assert shadow_class(tw) == "rnd-p5w"       # never "floor"
    assert P.policy_of(tw) == "rnd-p5w"
    clean = _book(store, _card("C"))
    assert shadow_class(clean) is None and P.policy_of(clean) is None


def test_book_policy_twins_books_all_three():
    store = _store()
    n = P.book_policy_twins(store, _card(), 1, 100.0, 75, None, 0.12,
                            0.18, 1.5, 2.5, CFG)
    assert n == 4
    by_tag = {P.policy_of(t): t for t in store.all() if P.policy_of(t)}
    assert sorted(by_tag) == ["rnd-base2", "rnd-p5t", "rnd-p5w", "rnd-plad"]
    # P-LAD and its dedicated baseline both book 2 lots (halving must be
    # physical); the full-exit policies match the clean fill's sizing.
    assert by_tag["rnd-plad"].lots == 2 and by_tag["rnd-base2"].lots == 2
    assert by_tag["rnd-p5w"].lots == 1


def _entered(store, tag, entered_at=E, px=100.0, mode=TradingMode.INTRADAY):
    tw = _book(store, _card(cid=tag, mode=mode, at=entered_at), tag)

    # store.all() hands out deep copies (house rule) — mutations must go
    # through apply_monitor, exactly as the paper loop's evaluate step does.
    def up(t):
        if t.id == tw.id:
            t.entered_at = entered_at
            t.current_premium = px

    store.apply_monitor(up)
    return tw


def test_p5w_books_target_inside_window():
    store = _store()
    tw = _entered(store, "rnd-p5w", px=106.0)
    P.apply_policy_exits(store, CFG, 0.0, now=E + 600)
    got = next(t for t in store.all() if t.id == tw.id)
    assert got.status.value == "exited"
    assert got.auto_close_reason == "rnd-p5w_target"
    assert got.exit_premium == 106.0


def test_p5w_closes_at_window_end_without_target():
    store = _store()
    tw = _entered(store, "rnd-p5w", px=102.0)
    P.apply_policy_exits(store, CFG, 0.0, now=E + 121 * 60)   # window over
    got = next(t for t in store.all() if t.id == tw.id)
    assert got.status.value == "exited"
    assert got.auto_close_reason == "rnd-p5w_window"


def test_p5t_holds_after_window():
    store = _store()
    tw = _entered(store, "rnd-p5t", px=102.0)
    P.apply_policy_exits(store, CFG, 0.0, now=E + 121 * 60)
    got = next(t for t in store.all() if t.id == tw.id)
    assert got.status.value == "entered"       # normal exits own it now
    # ...but the in-window target still books:
    tw2 = _entered(store, "rnd-p5t", entered_at=E + 1, px=106.0)
    P.apply_policy_exits(store, CFG, 0.0, now=E + 900)
    got2 = next(t for t in store.all() if t.id == tw2.id)
    assert got2.status.value == "exited"
    assert got2.auto_close_reason == "rnd-p5t_target"


def test_plad_books_half_once():
    store = _store()
    tw = _entered(store, "rnd-plad", px=106.0)
    P.apply_policy_exits(store, CFG, 0.0, now=E + 600)
    got = next(t for t in store.all() if t.id == tw.id)
    assert got.status.value == "partial"
    assert got.realized_pnl > 0
    booked_once = got.realized_pnl
    P.apply_policy_exits(store, CFG, 0.0, now=E + 660)   # must not re-book
    got = next(t for t in store.all() if t.id == tw.id)
    assert got.realized_pnl == booked_once


def test_positional_no_window_leaves_twin_to_normal_exits():
    late = int(datetime(2026, 8, 12, 15, 10, tzinfo=IST).timestamp())
    store = _store()
    tw = _entered(store, "rnd-p5w", entered_at=late, px=110.0,
                  mode=TradingMode.POSITIONAL)
    P.apply_policy_exits(store, CFG, 0.0, now=late + 300)
    got = next(t for t in store.all() if t.id == tw.id)
    assert got.status.value == "entered"       # vacuous window: policy inert


def test_policy_summary_pairs_and_gates_verdict():
    store = _store()
    for i in range(3):
        at = E + i * 1000
        card = _card(cid=f"S{i}", at=at)
        clean = _book(store, card)
        store.auto_close(clean.id, 103.0, "stop", price_source="simulated")
        tw = _book(store, card, "rnd-p5w")
        store.auto_close(tw.id, 105.0, "rnd-p5w_target", price_source="simulated")
    s = P.policy_summary(store, CFG)
    m = s["policies"]["rnd-p5w"]["modes"]["intraday"]
    assert m["pairs"] == 3 and m["diverged"] == 3
    assert m["avg_delta"] is not None and m["avg_delta"] > 0
    assert m["verdict"] is None                # 3 < 30: accumulating, no verdict
    assert s["min_diverged"] == 30


def test_closed_twins_never_reach_the_floor_ledger():
    """Review critical C1: with zero floor-vetoed fills, closed policy twins
    must leave summarize()['hollow'] empty — the floor's 30-fill verdict must
    never be graded on the exit-policy experiment's P&L."""
    from app.paper.service import summarize
    store = _store()
    for tag in ("rnd-p5w", "rnd-p5t"):
        tw = _book(store, _card(cid=tag), tag)
        store.auto_close(tw.id, 105.0, f"{tag}_target", price_source="simulated")
    s = summarize(store)
    hollow = s.get("hollow")
    assert not hollow or hollow.get("trades", 0) == 0, hollow


def test_divergence_is_economic_not_cosmetic():
    """Review criticals C2/C4: reason-only differences with identical money
    must NOT count as diverged; partial-leg money gaps with identical final
    legs MUST."""
    store = _store()
    # Same exit price, different reason strings -> identical economics.
    card = _card(cid="COSM")
    clean = _book(store, card)
    store.auto_close(clean.id, 104.0, "target1", price_source="simulated")
    tw = _book(store, card, "rnd-p5t")
    store.auto_close(tw.id, 104.0, "rnd-p5t_target", price_source="simulated")
    s = P.policy_summary(store, CFG)
    m = s["policies"]["rnd-p5t"]["modes"]["intraday"]
    assert m["pairs"] == 1 and m["diverged"] == 0

    # Partial-leg gap, identical final legs -> must diverge (P-LAD's whole
    # effect lives in the partial leg).
    store2 = _store()
    card2 = _card(cid="PLAD")
    base2 = _book(store2, card2, "rnd-base2")
    plad = _book(store2, card2, "rnd-plad")
    store2.book_partial(plad.id, 105.0, 0.5)     # policy banks at +5%
    store2.book_partial(base2.id, 112.0, 0.5)    # baseline banks at +12%
    store2.auto_close(plad.id, 100.0, "stop", price_source="simulated")
    store2.auto_close(base2.id, 100.0, "stop", price_source="simulated")
    s2 = P.policy_summary(store2, CFG)
    m2 = s2["policies"]["rnd-plad"]["modes"]["intraday"]
    assert m2["pairs"] == 1 and m2["diverged"] == 1
    assert m2["avg_delta"] < 0                    # baseline banked more


def test_scanner_has_no_mode_dimension():
    assert "mode" not in S.DIMS      # modes differ by construction, not condition


def test_scanner_requires_dev_test_sign_agreement(monkeypatch):
    dev, test = S.DEV_TEST_SPLIT - 5 * 86400, S.DEV_TEST_SPLIT + 5 * 86400

    def row(entered, tape, pnl):
        return {"id": "x", "mode": "intraday", "entered_at": entered,
                "entry_premium": 100.0, "quantity": 75, "initial_quantity": 75,
                "notes": "", "status": "exited", "tape_state": tape,
                "realized_pnl": pnl}

    rows = []
    # "developing" beats complement in BOTH eras (n=32 >= 30):
    for i in range(16):
        rows.append(row(dev + i, "developing", 800.0))
        rows.append(row(test + i, "developing", 700.0))
    # complement: flat-to-negative both eras
    for i in range(16):
        rows.append(row(dev + 100 + i, "stretched", -300.0))
        rows.append(row(test + 100 + i, "stretched", -200.0))
    monkeypatch.setattr(A, "_load_rows", lambda: rows)
    out = S.scan(CFG)
    keys = {(c["dim"], c["key"]) for c in out["candidates"]}
    assert ("tape_state", "developing") in keys
    # Now flip TEST-era sign for the cell: candidate must vanish.
    for r in rows:
        if r["tape_state"] == "developing" and r["entered_at"] >= S.DEV_TEST_SPLIT:
            r["realized_pnl"] = -900.0
    out = S.scan(CFG)
    keys = {(c["dim"], c["key"]) for c in out["candidates"]}
    assert ("tape_state", "developing") not in keys
    assert "pre-register" in out["note"]
