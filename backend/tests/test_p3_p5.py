"""P3-P5 batch tests: scalp mode, multi-day frames, retire pushes,
exit-type analytics, honest-era restatement.

Run:  python backend/tests/test_p3_p5.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

from app.config import Settings
from app.signals.models import (
    Action,
    Bias,
    Direction,
    MarketStatus,
    Regime,
    ScoreBreakdown,
    SignalCard,
    SignalResponse,
    SignalState,
    TradingMode,
)

BASE = int(time.time()) // 86400 * 86400 + 5 * 3600   # stable mid-day epoch


def _cfg(**over):
    return Settings(_env_file=None, **over)


def _card(mode=TradingMode.SCALP, entry=8.0, t1=8.48, lots=1, lot_size=65,
          direction=Direction.CE, at=BASE, cid="S"):
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=mode, title="t",
        action=Action.BUY_CE if direction is Direction.CE else Action.BUY_PE,
        direction=direction, state=SignalState.ACTIVE,
        contract=f"NIFTY 24000 {direction.value}", strike=24000.0, expiry="2026-07-28",
        entry_low=entry - 1, entry_high=entry + 1, premium_sl=entry * 0.92,
        target1=t1, target2=t1 * 1.1, trailing_sl_rule="r", risk_reward=0.75,
        confidence=82.0, underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 180,
        score=ScoreBreakdown(direction=direction, components=[], total=82.0, max=100),
        ref_entry_premium=entry, lot_size=lot_size, suggested_lots=lots,
    )


def _resp(card, at=BASE):
    # Bias follows the card so a PE response reads as a bearish tape — that is
    # what makes _trend_flipped see an actual flip against an active CE card.
    bias = Bias.BEARISH if card.direction is Direction.PE else Bias.BULLISH
    regime = Regime.MODERATE_BEARISH if bias is Bias.BEARISH else Regime.MODERATE_BULLISH
    return SignalResponse(
        symbol="NIFTY", mode=card.mode, evaluated_at=at,
        status=MarketStatus(
            symbol="NIFTY", mode=card.mode, regime=regime,
            regime_label="R", bias=bias, bull_score=60, bear_score=40,
            headline="h", vix_status=None, news_label=None, news_net=None, notes=[],
        ),
        action=card.action, signal=card, no_trade_reason=None, score=None,
    )


# ---- P3: scalp mode wiring -------------------------------------------------

def test_scalp_profile_gated_by_signal_modes():
    from app.signals.modes import build_profiles

    off = build_profiles(_cfg())
    assert "scalp" not in off, "scalp must stay off until SIGNAL_MODES asks for it"
    on = build_profiles(_cfg(SIGNAL_MODES="intraday,scalp,positional"))
    assert list(on) == ["intraday", "scalp", "positional"], list(on)
    p = on["scalp"]
    assert p.timeframe == "1m" and p.validity_seconds == 180 and p.score_valid == 80
    assert "paper" in p.label.lower()
    print("  MODES  -> scalp absent by default, full profile when enabled")


def test_scalp_friction_veto():
    from app.signals.service import SignalService

    class _Stub:
        cfg = _cfg()

    veto = SignalService._scalp_friction_veto
    # Rs 8 premium, 1 lot: two Rs 20 brokerage legs alone dwarf the Rs 31 move.
    v = veto(_Stub(), _card(entry=8.0, t1=8.48))
    assert v is not None and "friction" in v.lower(), v
    # Rs 100 premium, 10 lots: charges are ~5% of the T1 move — viable.
    assert veto(_Stub(), _card(entry=100.0, t1=106.0, lots=10)) is None
    # Non-scalp cards are never this method's business.
    assert veto(_Stub(), _card(mode=TradingMode.INTRADAY, entry=8.0, t1=8.48)) is None
    # Degenerate cards refuse to divide, not crash.
    assert veto(_Stub(), _card(entry=8.0, t1=8.48, lot_size=0)) is None
    assert veto(_Stub(), _card(entry=8.0, t1=7.0)) is None      # T1 below entry

    class _Off:
        cfg = _cfg(SCALP_MAX_FRICTION_PCT=0.0)
    assert veto(_Off(), _card(entry=8.0, t1=8.48)) is None, "0 must disable the veto"
    print(f"  VETO   -> tiny premium refused ('{v[:58]}…'), viable size passes")


def test_multi_day_15m_frame():
    from app.market.candles import CandleEngine

    def bars(day_offset, tf_s, n, start_min=9 * 60 + 15):
        day0 = (BASE + 19800) // 86400 - day_offset
        sod = day0 * 86400 - 19800
        return [{"ts": sod + start_min * 60 + i * tf_s, "open": 100, "high": 101,
                 "low": 99, "close": 100.5, "volume": 10} for i in range(n)]

    eng = CandleEngine(token=1)
    # Seed 15m with three days of history: all survive.
    eng.seed("15m", bars(2, 900, 25) + bars(1, 900, 25) + bars(0, 900, 25))
    assert len(eng.dataframe("15m")) == 75
    # Session frame seeded with yesterday+today keeps what it is given, but a
    # later OLDER-day seed for a session frame is refused while 15m accepts.
    eng.seed("3m", bars(0, 180, 40))
    assert len(eng.dataframe("3m")) == 40
    # Live tick today must NOT wipe the 15m history (same session day).
    eng.add_tick(102.0, 1000.0, BASE)
    assert len(eng.dataframe("15m")) >= 75, "multi-day frame lost history on a tick"
    # A NEXT-day tick clears session frames but keeps the 15m frame.
    eng.add_tick(103.0, 500.0, BASE + 86400)
    assert len(eng.dataframe("3m")) == 1, "session frame must reset on rollover"
    assert len(eng.dataframe("15m")) >= 75, "15m frame must survive the rollover"
    print("  15M    -> 75 seeded bars survive ticks and the day rollover; 3m resets")


def test_retire_push_fires_outside_lock():
    from app.signals.store import SignalStore, ThrottleConfig

    store = SignalStore(store_path=None)
    events = []
    store.notify = lambda card: events.append(("adopt", card.id)) or True
    store.notify_retire = lambda card, state: events.append((state, card.id))
    cfg = ThrottleConfig(max_per_day=10, min_gap_s=0, cooldown_s=0)

    c1 = _card(mode=TradingMode.INTRADAY, cid="A")
    store.reconcile(_resp(c1), BASE, cfg)
    assert events == [("adopt", c1.id)], events
    # Expiry retires the card and phones about it even with no fresh signal.
    empty = _resp(c1, at=BASE + 500)
    empty.signal = None
    empty.action = Action.WAIT
    store.reconcile(empty, BASE + 500, cfg)
    assert ("expired", c1.id) in events, events
    # Trend flip cancels and phones.
    c2 = _card(mode=TradingMode.INTRADAY, cid="B", at=BASE + 600)
    store.reconcile(_resp(c2, at=BASE + 600), BASE + 600, cfg)
    c3 = _card(mode=TradingMode.INTRADAY, direction=Direction.PE, cid="C", at=BASE + 700)
    store.reconcile(_resp(c3, at=BASE + 700), BASE + 700, cfg)
    assert ("cancelled", c2.id) in events, events
    # A notify_retire that raises must never break reconcile.
    store.notify_retire = lambda card, state: 1 / 0
    empty2 = _resp(c3, at=BASE + 5000)
    empty2.signal = None
    store.reconcile(empty2, BASE + 5000, cfg)
    print("  RETIRE -> expiry and flip both push; a raising hook is contained")


# ---- P4: exit-type analytics + honest era ----------------------------------

def _trade(tid, reason=None, auto=True, pnl=1000.0, entry=100.0, mfe=None,
           entered_at=None, status="exited", exit_reason=None):
    from app.trades.models import Trade, TradeStatus

    at = entered_at or BASE
    qty = 130
    return Trade(
        id=tid, symbol="NIFTY", mode=TradingMode.INTRADAY, direction=Direction.CE,
        contract="NIFTY 24000 CE", strike=24000.0, entry_premium=entry, lots=2,
        lot_size=65, quantity=qty, initial_quantity=qty, status=TradeStatus(status),
        stop_loss=entry * 0.82, target1=entry * 1.27, target2=entry * 1.54,
        trailing_sl=entry * 0.82, created_at=at, entered_at=at,
        exited_at=at + 1800 if status == "exited" else None,
        exit_premium=entry + pnl / qty if status == "exited" else None,
        realized_pnl=pnl, auto_closed=auto, auto_close_reason=reason,
        mfe_premium=mfe, excursion_from=at, exit_reason=exit_reason,
    )


def test_analytics_groups_and_flags():
    from app.trades.analytics import exit_type, summarize

    rows = [
        _trade("t1", reason="stop", pnl=-2000.0, mfe=104.0),
        _trade("t2", reason="stop", pnl=-1800.0, mfe=100.0),
        _trade("t3", reason="target1", pnl=2600.0, mfe=128.0),
        _trade("t4", reason="broker flat", pnl=500.0, mfe=110.0),          # unexplained
        _trade("t5", reason="broker flat", pnl=900.0, exit_reason="broker stop"),
        _trade("t6", reason=None, auto=False, pnl=300.0),                  # manual
        _trade("t7", reason=None, auto=False, pnl=0.0, status="entered"),  # open: excluded
    ]
    assert exit_type(rows[0]) == "stop"
    assert exit_type(rows[3]) == "broker_flat"
    assert exit_type(rows[4]) == "broker_flat", "user reason must not change the group"
    assert exit_type(rows[5]) == "manual"

    s = summarize(rows)
    assert s["n_closed"] == 6
    assert s["pnl_basis"] == "gross_of_charges", s["pnl_basis"]
    by = {g["exit_type"]: g for g in s["by_exit_type"]}
    assert by["stop"]["n"] == 2 and by["stop"]["wins"] == 0
    assert by["stop"]["expectancy"] == -1900.0
    assert by["broker_flat"]["n"] == 2 and by["broker_flat"]["win_rate"] == 1.0
    # t3: potential (128-100)*130 = 3640, realised 2600 -> ~71.4% capture.
    assert abs(by["target1"]["capture_pct"] - 71.4) < 0.2, by["target1"]
    # t2 never went positive -> no capture sample for its group.
    # t4 = unreasoned broker-flat; t6 = unreasoned MANUAL exit — included since
    # 28-Jul (the manual bucket was the costliest and had no reason capture).
    assert set(s["needs_reason"]) == {"t4", "t6"}, s["needs_reason"]
    # The recorded reason splits the broker_flat bucket — that split is the
    # entire point of asking. t4 (no answer yet) lands under "unexplained".
    br = by["broker_flat"]["by_reason"]
    assert br["broker stop"]["n"] == 1 and br["broker stop"]["total_pnl"] == 900.0
    assert br["unexplained"]["n"] == 1 and br["unexplained"]["total_pnl"] == 500.0
    print("  EXPECT -> grouped expectancy, 71.4% capture, t4 flagged, reasons split")


def test_analytics_capture_needs_entry_coverage():
    from app.trades.analytics import summarize

    # Tracking began 2 minutes into the trade: MFE is a tail, capture is a lie.
    late = _trade("late", reason="target1", pnl=2600.0, mfe=128.0)
    late.excursion_from = late.entered_at + 120
    never = _trade("never", reason="target1", pnl=2600.0, mfe=128.0)
    never.excursion_from = None
    s = summarize([late, never])
    assert s["by_exit_type"][0]["capture_pct"] is None, s["by_exit_type"][0]
    print("  COVER  -> late/absent excursion tracking yields no capture number")


def test_analytics_net_of_charges_flips_marginal_wins():
    from app.paper import charges as chg
    from app.trades.analytics import summarize

    # Gross +Rs 50.25 on one lot of 75 — charges Rs ~65 make it a net loss.
    t = _trade("m", reason="target1", pnl=50.25, entry=100.0)
    t.quantity = t.initial_quantity = 75
    t.exit_premium = 100.67
    fn = lambda x: chg.charges(x.entry_premium, x.exit_premium, x.initial_quantity, 2)
    gross = summarize([t])
    net = summarize([t], charges_fn=fn)
    assert gross["by_exit_type"][0]["wins"] == 1
    assert net["pnl_basis"] == "net_of_charges"
    assert net["by_exit_type"][0]["wins"] == 0, net["by_exit_type"][0]
    assert net["by_exit_type"][0]["total_pnl"] < 0
    print(f"  NET    -> gross +50.25 becomes net {net['by_exit_type'][0]['total_pnl']}: a loss")


def test_paper_capacity_is_per_mode():
    import tempfile
    from pathlib import Path

    import app.paper.service as paper_svc
    from app.paper.service import PaperTradingService
    from app.signals.risk_limits import RiskLimitStore
    from app.state import MarketState
    from app.trades.store import TradeStore

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        state = MarketState()
        cfg = _cfg(MAX_OPEN_POSITIONS=1, SIGNAL_MAX_PREMIUM_AGE_S=0)
        svc = PaperTradingService(cfg, state, store)
        # consider() reads risk_limit_store from its OWN module namespace, which
        # test_paper.py rebinds at import — pin the cap on the object consider()
        # actually reads (not the shared singleton), or this no-ops under a full
        # `pytest` collection and the assertion passes for the wrong reason.
        capped = RiskLimitStore(path=None)
        capped.set_many({"max_open_positions": 1}, cfg)
        prev = paper_svc.risk_limit_store
        paper_svc.risk_limit_store = capped
        # Fill the single INTRADAY slot.
        occupier = _card(mode=TradingMode.INTRADAY, entry=100.0, t1=127.0, cid="OCC")
        store.create_from_signal(occupier, 1, 100.0, 65)
        # A scalp card in-zone with a live tick must still fill: its mode's
        # book is empty, and its evidence must not queue behind intraday's.
        # Stamped NOW — consider() drops cards whose validity has lapsed, and
        # BASE (a fixed mid-day epoch) is hours old by an evening test run.
        now = int(time.time())
        scalp = _card(entry=100.0, t1=106.0, cid="SC", at=now)
        scalp.token = 4242
        state.ticks[4242] = {"last_price": 100.5}
        svc.consider(scalp)
        modes_open = sorted(t.mode.value for t in store.all()
                            if t.status.value in ("entered", "partial"))
        assert modes_open == ["intraday", "scalp"], modes_open
        # A SECOND scalp card now queues behind the first scalp fill.
        scalp2 = _card(entry=100.0, t1=106.0, cid="SC2", at=now + 1)
        scalp2.token = 4242
        svc.consider(scalp2)
        try:
            n_scalp = sum(1 for t in store.all() if t.mode.value == "scalp")
            assert n_scalp == 1, f"second scalp fill should defer, got {n_scalp}"
        finally:
            paper_svc.risk_limit_store = prev
        print("  SLOTS  -> full intraday book no longer blocks the scalp sample")


def test_exit_reason_store_roundtrip():
    import tempfile
    from pathlib import Path

    from app.trades.store import TradeStore

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "j.json")
        card = _card(mode=TradingMode.INTRADAY, entry=100.0, t1=127.0, lots=2)
        t = store.create_from_signal(card, 2, 100.0, 65)
        assert store.set_exit_reason(t.id, "fear exit") is None, "open rows have no exit"
        store.auto_close(t.id, 104.0, "broker flat", price_source="broker")
        got = store.set_exit_reason(t.id, "  broker stop  ")
        assert got is not None and got.exit_reason == "broker stop"
        assert any(e.kind == "exit_reason" for e in got.events)
        assert store.set_exit_reason(t.id, "   ") is None
        # Reopen wipes the reason with the exit it described.
        reopened = store.reopen(t.id)
        assert reopened is not None and reopened.exit_reason is None
        print("  REASON -> open refused, trimmed save, event journaled, reopen clears")


def test_paper_summary_honest_era_split():
    import tempfile
    from pathlib import Path

    from app.paper.service import HONEST_FILLS_FROM, summarize
    from app.trades.store import TradeStore

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        for tid, when, px in (("old", HONEST_FILLS_FROM - 86400, 130.0),
                              ("new", HONEST_FILLS_FROM + 86400, 104.0)):
            card = _card(mode=TradingMode.INTRADAY, entry=100.0, t1=127.0, lots=2)
            t = store.create_from_signal(card, 2, 100.0, 65)
            store.auto_close(t.id, px, "target1")
            # Backdate the LIVE row: get()/all() return copies by design, so
            # the era stamp must be planted on the stored object itself.
            with store._lock:
                store._trades[t.id].entered_at = when
                store._trades[t.id].exited_at = when + 900

        s = summarize(store)
        assert s["trades"] == 1, f"aggregates must count the honest era only: {s['trades']}"
        assert s["inflated_trades"] == 1
        assert len(s["rows"]) == 2, "the ledger itself keeps every row"
        assert s["rows"][-1]["era"].startswith("inflated"), s["rows"][-1]["era"]
        assert s["rows"][0]["era"] == "honest"
        # The +Rs 3900 fictional win is OUT of net_pnl; only the honest +Rs 520-ish is in.
        assert s["net_pnl"] < 600, s["net_pnl"]
        assert "excluded" in s["note"]
        print(f"  ERA    -> honest n=1 net {s['net_pnl']}, inflated row kept but flagged")


def test_calibration_skips_inflated_era():
    from app.paper.service import HONEST_FILLS_FROM
    from app.signals.calibration import _MIN_SAMPLES, _p75_mfe_pct

    def sample(i, when):
        return _trade(f"c{i}", reason="stop", pnl=-500.0, mfe=118.0, entered_at=when)

    old = [sample(i, HONEST_FILLS_FROM - 86400) for i in range(_MIN_SAMPLES)]
    assert _p75_mfe_pct(old) is None, "inflated-era rows must not reach the sample"
    new = [sample(i + 100, HONEST_FILLS_FROM + 86400) for i in range(_MIN_SAMPLES)]
    p75 = _p75_mfe_pct(new)
    assert p75 is not None and abs(p75 - 0.18) < 1e-9, p75
    print("  CALIB  -> 30 pre-era samples rejected; same 30 post-era accepted")


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
