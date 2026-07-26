"""Bank-the-quick-target prototype tests: the trigger and the paired A/B.

The policy: on a SINGLE-LOT trade, exit everything at the quick target instead
of going risk-free and trailing. The evidence: summarize()'s exit_ab block
reconstructs the banked variant's exit from each recorded row (the policies
are identical until the quick target trades), giving a paired head-to-head on
the same fills. These tests pin the trigger's guardrails and the A/B math.

Run:  python backend/tests/test_quick_bank.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
import time
from pathlib import Path

from app.paper import charges as chg
from app.paper.service import HONEST_FILLS_FROM, summarize
from app.signals.models import (
    Action,
    Direction,
    ScoreBreakdown,
    SignalCard,
    SignalState,
    TradingMode,
)
from app.trades import monitor
from app.trades.models import TradeStatus
from app.trades.store import TradeStore


def _card(cid="Q", entry=100.0, at=None):
    at = at or int(time.time())
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24000 CE", strike=24000.0, expiry="2026-07-30",
        entry_low=entry - 1, entry_high=entry + 1, premium_sl=round(entry * 0.82, 2),
        target1=round(entry * 1.27, 2), target2=round(entry * 1.45, 2),
        trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=[], total=81.0),
    )


def _store():
    d = tempfile.mkdtemp()
    return TradeStore(path=Path(d) / "p.json")


def _mk(store, cid, lots=1, entry=100.0, quick_pct=0.12):
    return store.create_from_signal(_card(cid, entry), lots, entry, 65,
                                    quick_pct=quick_pct)


def test_trigger_guardrails():
    """quick_bank fires only when enabled, at/above QT, on an initially
    single-lot position — and losses still outrank it."""
    s = _store()
    t = _mk(s, "A")                                   # 1 lot, QT = 112
    t.current_premium = 113.0
    # Not in the enabled set -> never fires (default-off posture).
    assert monitor.auto_close_trigger(t, {"stop", "target1"}) != "quick_bank"
    on = {"stop", "invalidation", "quick_bank", "target1", "target2"}
    assert monitor.auto_close_trigger(t, on) == "quick_bank"
    # Below the quick target: nothing.
    t.current_premium = 111.0
    assert monitor.auto_close_trigger(t, on) is None
    # Beats target1 when a bar reaches both — the policy exits at first touch.
    t.current_premium = 130.0
    assert monitor.auto_close_trigger(t, on) == "quick_bank"
    # Stop outranks: a bar that hits both records the loss.
    t.current_premium = 113.0
    from app.trades.models import TradeAction
    t.recommendation = TradeAction.STOPLOSS
    assert monitor.auto_close_trigger(t, on) == "stop"
    t.recommendation = TradeAction.HOLD
    # Multi-lot positions are not this policy's business (they book half).
    two = _mk(s, "B", lots=2)
    two.current_premium = 113.0
    assert monitor.auto_close_trigger(two, on) is None
    # POSITIONAL is out of scope: banking a multi-day thesis at +12% forfeits
    # the swing the mode exists to ride (the retro book's one positional
    # runner: +60% ridden vs +12% banked).
    pos_card = _card("P")
    pos_card.mode = TradingMode.POSITIONAL
    pos = s.create_from_signal(pos_card, 1, 100.0, 65, quick_pct=0.12)
    pos.current_premium = 113.0
    assert monitor.auto_close_trigger(pos, on) is None
    # A HALF-BOOKED 2-lot row has lots==1 but initial_quantity==2*65 — still
    # excluded: it already banked at the quick target the multi-lot way.
    s.book_partial(two.id, 113.0, 0.5)
    half = s.get(two.id)
    half.current_premium = 113.0
    assert monitor.auto_close_trigger(half, on) is None
    print("  BANK   -> fires only enabled+QT+single-lot; stop outranks; multi-lot exempt")


def test_paper_flag_wires_the_trigger():
    """QUICK_BANK_SINGLE_LOT=true adds quick_bank to the paper trigger set."""
    from app.config import Settings
    from app.paper.service import _EXIT_TRIGGERS

    assert "quick_bank" not in _EXIT_TRIGGERS, "must be opt-in, not default"
    cfg_off = Settings(_env_file=None)
    cfg_on = Settings(_env_file=None, QUICK_BANK_SINGLE_LOT=True)
    assert cfg_off.quick_bank_single_lot is False
    assert cfg_on.quick_bank_single_lot is True
    print("  FLAG   -> default off; opt-in via QUICK_BANK_SINGLE_LOT")


def test_exit_ab_paired_math():
    s = _store()
    # Winner the ratchet rode to T1 (+27%): banking would have capped it at QT.
    w = _mk(s, "W")
    monitor.evaluate(w, 113.0, None, 690, "2026-07-25", 0)   # latches t0
    s.apply_monitor(lambda t: monitor.evaluate(
        t, 113.0, None, 690, "2026-07-25", 0) if t.id == w.id else None)
    s.auto_close(w.id, 127.0, "target1")
    # Give-back the ratchet surrendered to ~breakeven: banking takes +12%.
    g = _mk(s, "G")
    s.apply_monitor(lambda t: monitor.evaluate(
        t, 113.0, None, 690, "2026-07-25", 0) if t.id == g.id else None)
    s.auto_close(g.id, 100.1, "stop")
    # Never reached QT: both policies identical.
    n = _mk(s, "N")
    s.auto_close(n.id, 95.0, "stop")

    out = summarize(s, exit_slippage_pct=0.0)
    ab = out["exit_ab"]
    assert ab is not None and ab["policy_live"] == "ratchet"
    assert ab["n"] == 3 and ab["n_diverged"] == 2, ab

    qty = 65
    # Expected: variant banks BOTH diverged trades at the RECORDED CROSS
    # premium (113.0 — the observed tape at the t0 latch), not the 112 level.
    a_tot = round(chg.net_pnl(100.0, 127.0, qty) + chg.net_pnl(100.0, 100.1, qty)
                  + chg.net_pnl(100.0, 95.0, qty), 2)
    v_tot = round(chg.net_pnl(100.0, 113.0, qty) * 2 + chg.net_pnl(100.0, 95.0, qty), 2)
    assert abs(ab["ratchet"]["net_pnl"] - a_tot) < 0.01, (ab["ratchet"], a_tot)
    assert abs(ab["quick_bank"]["net_pnl"] - v_tot) < 0.01, (ab["quick_bank"], v_tot)
    assert abs(ab["delta_net"] - round(v_tot - a_tot, 2)) < 0.01
    assert "evidence gathering" in ab["verdict"], ab["verdict"]
    print(f"  ABMATH -> ratchet ₹{a_tot} vs banked ₹{v_tot} on identical fills")


def test_exit_ab_slippage_and_exclusions():
    s = _store()
    # Hollow row: excluded from the A/B entirely.
    h = _mk(s, "H")
    s.update(h.id, notes="hollow: vol floor")
    monitor.evaluate(s.get(h.id), 113.0, None, 690, "2026-07-25", 0)
    s.apply_monitor(lambda t: monitor.evaluate(
        t, 113.0, None, 690, "2026-07-25", 0) if t.id == h.id else None)
    s.auto_close(h.id, 100.1, "stop")
    # Clean diverged row.
    g = _mk(s, "G2")
    s.apply_monitor(lambda t: monitor.evaluate(
        t, 113.0, None, 690, "2026-07-25", 0) if t.id == g.id else None)
    s.auto_close(g.id, 100.1, "stop")

    out = summarize(s, exit_slippage_pct=0.004)
    ab = out["exit_ab"]
    assert ab["n"] == 1, f"hollow row must not enter the A/B: {ab['n']}"
    # Variant fill = the recorded cross (113.0) with exit slippage applied.
    v = chg.net_pnl(100.0, round(113.0 * 0.996, 2), 65)
    assert abs(ab["quick_bank"]["net_pnl"] - v) < 0.01, (ab["quick_bank"], v)
    print("  ABEDGE -> hollow rows excluded; variant fill carries exit slippage")


def test_live_banked_rows_stay_out_of_both_arms():
    """THE self-grading trap (review-confirmed critical): a row the live
    quick_bank trigger closed must not be credited to the RATCHET arm after
    the flag is flipped back off — its ratchet path was never observed."""
    s = _store()
    b = _mk(s, "LB")
    s.apply_monitor(lambda t: monitor.evaluate(
        t, 113.0, None, 690, "2026-07-25", 0) if t.id == b.id else None)
    s.auto_close(b.id, 112.9, "quick_bank")          # the live trigger's own close
    clean = _mk(s, "CL")
    s.auto_close(clean.id, 95.0, "stop")

    ab = summarize(s, exit_slippage_pct=0.0)["exit_ab"]
    assert ab["policy_live"] == "ratchet"
    assert ab["n"] == 1, f"banked row must be in NEITHER arm: {ab['n']}"
    assert ab["banked_live_excluded"] == 1, ab
    # The ratchet arm must contain only the clean stop, not the banked fill.
    assert abs(ab["ratchet"]["net_pnl"] - chg.net_pnl(100.0, 95.0, 65)) < 0.01
    print("  ABSELF -> a live-banked fill can never grade the ratchet arm")


def test_variant_prices_gap_throughs_at_the_cross():
    """Review-confirmed major: a premium that GAPS past the quick target fills
    the banking exit at the observed tape, not the level. evaluate() records
    t0_cross_premium at the latch; the A/B must price the variant from it."""
    s = _store()
    g = _mk(s, "GAP")
    # One monitor cycle jumps straight from below QT (112) to 140.
    s.apply_monitor(lambda t: monitor.evaluate(
        t, 140.0, None, 690, "2026-07-25", 0) if t.id == g.id else None)
    row = s.get(g.id)
    assert row.t0_hit and row.t0_cross_premium == 140.0, row.t0_cross_premium
    s.auto_close(g.id, 100.1, "stop")                # ratchet later gave it back

    ab = summarize(s, exit_slippage_pct=0.004)["exit_ab"]
    v = chg.net_pnl(100.0, round(140.0 * 0.996, 2), 65)
    assert abs(ab["quick_bank"]["net_pnl"] - v) < 0.01, (ab["quick_bank"], v)
    print("  ABGAP  -> gap-through variant priced at the 140 cross, not the 112 level")


def test_live_journal_opt_in_is_functional():
    """Review-confirmed major: AUTO_CLOSE_TRIGGERS=...,quick_bank must survive
    the validation intersection — a documented opt-in that silently no-ops is
    worse than none."""
    assert "quick_bank" in monitor.AUTO_CLOSE_NAMES
    from app.config import Settings
    from app.trades.service import TradeMonitorService
    import app.trades.service as tsvc
    svc = TradeMonitorService.__new__(TradeMonitorService)
    orig = tsvc.get_settings
    tsvc.get_settings = lambda: Settings(
        _env_file=None, AUTO_CLOSE_JOURNAL=True,
        AUTO_CLOSE_TRIGGERS="stop,quick_bank")
    try:
        assert "quick_bank" in svc._auto_close_set(), svc._auto_close_set()
    finally:
        tsvc.get_settings = orig
    print("  OPTIN  -> AUTO_CLOSE_TRIGGERS accepts quick_bank end-to-end")


def test_unprovable_single_lot_is_refused():
    """Review-confirmed minor: a legacy row with initial_quantity=None whose
    quantity happens to equal lot_size (a de-risked multi-lot) must not bank."""
    s = _store()
    t = _mk(s, "LEG")
    t.initial_quantity = None                        # legacy journal shape
    t.quantity = 65                                  # == lot_size after a partial
    t.current_premium = 113.0
    on = {"quick_bank"}
    assert monitor.auto_close_trigger(t, on) is None
    print("  LEGACY -> unprovable single-lot rows are exempt, not assumed")


def test_exit_ab_unmeasurable_when_banking_live():
    s = _store()
    g = _mk(s, "L")
    s.auto_close(g.id, 95.0, "stop")
    ab = summarize(s, quick_bank_live=True)["exit_ab"]
    assert ab["policy_live"] == "quick_bank" and "unobservable" in ab["note"]
    assert "ratchet" not in ab, "must not fabricate a counterfactual it cannot observe"
    print("  ABLIVE -> with banking live, the A/B says so instead of inventing numbers")


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
