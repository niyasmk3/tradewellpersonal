"""P1-4 v1: the VWAP-cross setup detector and its shadow ledger.

The detector's parameters are FROZEN at registration (setups.py) — these
tests pin the registered behaviour: >=8 closes on one side of session VWAP,
a cross on >=1.2x median volume, both directions, session-scoped VWAP.
The wiring tests prove the card goes through the REAL liquidity/freshness
gates and lands in its own ledger without touching any other class.

Run:  python backend/tests/test_setup_detector.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
import time
from pathlib import Path

import pandas as pd

from app.config import Settings
from app.signals.models import Direction, TradingMode
from app.signals.setups import DEDUPE_S, SETUP_VWAP, vwap_cross
from app.trades.store import TradeStore

IST = 19800


def _bar_ts(day_offset: int, bar: int) -> int:
    base = (int(time.time()) + IST) // 86400 * 86400 - IST
    return base - day_offset * 86400 + 9 * 3600 + 15 * 60 + bar * 180


def _frame(closes_vs_vwap, cross_up=True, cross_vol=200.0, base_vol=100.0,
           day_offset=0):
    """Bars whose closes sit below (or above) their running VWAP for the
    prior run, then a cross bar. Prices are engineered around a flat 100
    so the typical-price VWAP stays near 100 while closes straddle it."""
    rows = []
    n_prior = closes_vs_vwap
    for b in range(n_prior + 12):
        px = 100.0
        rows.append({"ts": _bar_ts(day_offset, b), "open": px, "high": px + 0.4,
                     "low": px - 0.4, "close": px, "volume": base_vol})
    # The run: closes clearly on one side while highs/lows keep VWAP ~100.
    # It occupies the n_prior bars BEFORE the final (cross) bar.
    side = -0.3 if cross_up else 0.3
    for k in range(n_prior):
        rows[-(n_prior + 1) + k]["close"] = 100.0 + side
    # Cross bar: close through the VWAP on volume.
    last = rows[-1]
    last["close"] = 100.0 - side * 2
    last["high"] = max(last["high"], last["close"] + 0.1)
    last["low"] = min(last["low"], last["close"] - 0.1)
    last["volume"] = cross_vol
    return pd.DataFrame(rows)


def test_detector_fires_both_directions():
    hit = vwap_cross(_frame(8, cross_up=True))
    assert hit is not None and hit.direction is Direction.CE, hit
    assert hit.name == SETUP_VWAP and "reclaimed" in hit.note
    hit = vwap_cross(_frame(8, cross_up=False))
    assert hit is not None and hit.direction is Direction.PE
    assert "lost" in hit.note
    print("  FIRE   -> reclaim -> CE, reject -> PE; no regime lockout")


def test_detector_registered_gates():
    # Only 5 bars on one side: the run requirement fails.
    short_run = _frame(8, cross_up=True)
    for i in range(-9, -6):              # break the run's oldest bars
        short_run.loc[len(short_run) + i, "close"] = 100.3
    assert vwap_cross(short_run) is None
    # Cross on WEAK volume: the participation requirement fails.
    assert vwap_cross(_frame(8, cross_up=True, cross_vol=110.0)) is None
    # Too few bars: warm-up.
    assert vwap_cross(_frame(8).head(6)) is None
    # Volume-less frame (indices/holes): nothing to measure.
    dead = _frame(8)
    dead["volume"] = 0.0
    assert vwap_cross(dead) is None
    print("  GATES  -> run length, volume mult and warm-up all enforced")


def test_detector_vwap_is_session_scoped():
    """Yesterday's bars must not leak into today's VWAP: prepend a prior day
    trading far away — the detection on today's bars must be unchanged."""
    today = _frame(8, cross_up=True)
    yday = _frame(8, cross_up=True, day_offset=1)
    yday["open"] = yday["open"] + 500     # a very different prior session
    yday["high"] = yday["high"] + 500
    yday["low"] = yday["low"] + 500
    yday["close"] = yday["close"] + 500
    both = pd.concat([yday, today], ignore_index=True)
    hit = vwap_cross(both)
    assert hit is not None and hit.direction is Direction.CE, \
        "prior session leaked into session VWAP"
    print("  SESSION-> VWAP resets at the day boundary")


# ---- service wiring ----------------------------------------------------------

class _Snap:
    def __init__(self, ltp, fut):
        self.ltp, self.fut_ltp = ltp, fut


class _Row:
    def __init__(self, strike, ce_ltp, pe_ltp, token=901):
        self.strike = strike
        self.ce_ltp, self.pe_ltp = ce_ltp, pe_ltp
        self.ce_token, self.pe_token = token, token + 1
        self.ce_oi = self.pe_oi = 500000.0
        self.ce_oi_change = self.pe_oi_change = 0.0
        self.ce_volume = self.pe_volume = 1e6
        self.ce_iv = self.pe_iv = None


class _Chain:
    expiry = "2026-08-06"
    atm_strike = 24600.0
    pcr = 1.0

    def __init__(self, rows):
        self.rows = rows
        self.updated_at = int(time.time())


class _State:
    def __init__(self):
        now = int(time.time())
        self.chain = _Chain([_Row(24600.0, 100.0, 100.0)])
        self.ticks = {901: {"last_price": 100.0, "ts": now},
                      902: {"last_price": 100.0, "ts": now}}

    def underlying_snapshot(self, symbol):
        return _Snap(24600.0, 24660.0)

    def get_option_chain(self, key):
        return self.chain


def _svc(**over):
    from app.signals.service import SignalService

    svc = SignalService.__new__(SignalService)
    # PAPER_TRADING on: the booking gate short-circuits without a paper book
    # (correct live), and a vacuous early return here would let the cutoff/
    # staleness assertions below pass for the wrong reason.
    svc.cfg = Settings(_env_file=None, SIGNAL_POST_GAP_QUIET_S=0,
                       PAPER_TRADING=True, **over)
    svc.state = _State()
    return svc


def _fresh(mode=TradingMode.INTRADAY):
    from app.signals.models import (
        Action, Bias, MarketStatus, Regime, SignalResponse,
    )

    return SignalResponse(
        symbol="NIFTY", mode=mode, evaluated_at=int(time.time()),
        status=MarketStatus(
            symbol="NIFTY", mode=mode, regime=Regime.SIDEWAYS,
            regime_label="R", bias=Bias.NEUTRAL, bull_score=50, bear_score=40,
            headline="h", vix_status=None, news_label=None, news_net=None,
            notes=[]),
        action=Action.WAIT, signal=None, no_trade_reason="Score 50 below 70",
        score=None)


def test_setup_shadow_books_through_real_gates():
    import app.signals.store as store_mod
    from app.signals.store import SignalStore

    svc = _svc()
    tmp_store = SignalStore(store_path=None)
    prev = store_mod.setup_shadow_store
    store_mod.setup_shadow_store = tmp_store
    try:
        # 11:00 IST today — the real clock may sit past the 14:15 cutoff,
        # which correctly blocks bookings (covered by the guard test below).
        now = ((int(time.time()) + IST) // 86400) * 86400 - IST + 11 * 3600
        df = _frame(8, cross_up=True)
        name = svc._maybe_setup_shadow("NIFTY", _profile(svc), df, _fresh(), now)
        assert name == SETUP_VWAP
        held = tmp_store.latest("NIFTY", TradingMode.INTRADAY)
        assert held is not None and held.signal is not None
        card = held.signal
        assert card.hollow_reason.startswith("setup: vwap_cross")
        assert card.direction is Direction.CE and card.strike == 24600.0
        # lot_size resolves from the live instruments universe (absent in
        # this fake state) — the token is the wiring under test here.
        assert card.token == 901
        assert card.confidence == 0.0 and "(setup)" in card.title
        # Same bar again: deduped. New bar inside DEDUPE_S: deduped.
        assert svc._maybe_setup_shadow("NIFTY", _profile(svc), df, _fresh(), now + 5) is None
        df2 = df.copy()
        df2["ts"] = df2["ts"] + 180
        assert svc._maybe_setup_shadow("NIFTY", _profile(svc), df2, _fresh(), now + 200) is None
        # Past DEDUPE_S with a new bar: fires again.
        assert svc._maybe_setup_shadow(
            "NIFTY", _profile(svc), df2, _fresh(), now + DEDUPE_S + 10) == SETUP_VWAP
    finally:
        store_mod.setup_shadow_store = prev
    print("  WIRE   -> real strike/freshness gates; own store; bar+time dedupe")


def _profile(svc):
    from app.signals.modes import build_profiles

    return build_profiles(svc.cfg)[TradingMode.INTRADAY]


def test_setup_respects_mode_cutoff_and_staleness():
    import app.signals.store as store_mod
    from app.signals.store import SignalStore

    svc = _svc()
    tmp_store = SignalStore(store_path=None)
    prev = store_mod.setup_shadow_store
    store_mod.setup_shadow_store = prev.__class__(store_path=None)
    store_mod.setup_shadow_store = tmp_store
    try:
        df = _frame(8, cross_up=True)
        # Positional/scalp modes: v1 is intraday-only (the sized frame).
        from app.signals.modes import build_profiles
        profs = build_profiles(svc.cfg)
        if TradingMode.POSITIONAL in profs:
            assert svc._maybe_setup_shadow(
                "NIFTY", profs[TradingMode.POSITIONAL], df,
                _fresh(TradingMode.POSITIONAL), int(time.time())) is None
        # Past the 14:15 cutoff: no booking (confounded with the late gate).
        day = (int(time.time()) + IST) // 86400
        late_now = day * 86400 - IST + 14 * 3600 + 30 * 60
        assert svc._maybe_setup_shadow(
            "NIFTY", _profile(svc), df, _fresh(), late_now) is None
        # Stale premium: the freshness gate refuses the card.
        svc2 = _svc()
        svc2.state.ticks[901]["ts"] = int(time.time()) - 3600
        assert svc2._maybe_setup_shadow(
            "NIFTY", _profile(svc2), df, _fresh(), int(time.time())) is None
        assert len(tmp_store.history("NIFTY", TradingMode.INTRADAY)) == 0
        assert tmp_store.latest("NIFTY", TradingMode.INTRADAY) is None
    finally:
        store_mod.setup_shadow_store = prev
    print("  GUARD  -> intraday-only, cutoff-blocked, stale quotes refused")


def test_setup_refuses_bypassed_liquidity_guards():
    """Review catch (critical): the refusal read guards_bypassed off the
    StrikePick while only a local variable in strike.py ever knew — an
    illiquid ATM fallback booked into the evidence ledger anyway. The flag
    now rides the model; a low-OI chain must book NOTHING."""
    import app.signals.store as store_mod
    from app.signals.store import SignalStore

    svc = _svc()
    row = _Row(24600.0, 100.0, 100.0)
    row.ce_oi = row.pe_oi = 10.0          # far below the 100k OI floor
    svc.state.chain = _Chain([row])
    tmp = SignalStore(store_path=None)
    prev = store_mod.setup_shadow_store
    store_mod.setup_shadow_store = tmp
    try:
        now = ((int(time.time()) + IST) // 86400) * 86400 - IST + 11 * 3600
        assert svc._maybe_setup_shadow(
            "NIFTY", _profile(svc), _frame(8, cross_up=True), _fresh(), now) is None
        assert tmp.latest("NIFTY", TradingMode.INTRADAY) is None
    finally:
        store_mod.setup_shadow_store = prev
    print("  LIQUID -> guards-bypassed picks are refused, not ledgered")


def test_setup_frame_integrity_and_no_flip_guard():
    """Review catches: (a) a stale/yesterday frame must book nothing — the
    engine's own data-integrity refusal applies to setups verbatim; (b) an
    opposite-direction setup minutes later must NOT be blocked by the live
    feed's flip guard (that would re-import the direction lockout)."""
    import app.signals.store as store_mod
    from app.signals.store import SignalStore

    svc = _svc()
    tmp = SignalStore(store_path=None)
    prev = store_mod.setup_shadow_store
    store_mod.setup_shadow_store = tmp
    try:
        now = ((int(time.time()) + IST) // 86400) * 86400 - IST + 11 * 3600
        # (a) yesterday's frame: detector-worthy shape, integrity-refused.
        stale = _frame(8, cross_up=True, day_offset=1)
        assert svc._maybe_setup_shadow(
            "NIFTY", _profile(svc), stale, _fresh(), now) is None
        assert tmp.latest("NIFTY", TradingMode.INTRADAY) is None
        # (b) CE books, then a PE cross 3 minutes later ALSO books.
        assert svc._maybe_setup_shadow(
            "NIFTY", _profile(svc), _frame(8, cross_up=True), _fresh(), now) == SETUP_VWAP
        pe_df = _frame(8, cross_up=False)
        pe_df["ts"] = pe_df["ts"] + 180
        assert svc._maybe_setup_shadow(
            "NIFTY", _profile(svc), pe_df, _fresh(), now + 180) == SETUP_VWAP, \
            "flip guard must not apply to the setup ledger"
    finally:
        store_mod.setup_shadow_store = prev
    print("  FLIP   -> stale frames refused; CE then PE both book")


def test_setup_ledger_is_isolated():
    from app.paper.service import shadow_class, summarize
    from app.signals.models import (
        Action, ScoreBreakdown, SignalCard, SignalState,
    )

    at = int(time.time())
    card = SignalCard(
        id=f"S-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24600 CE", strike=24600.0, expiry="2026-08-06",
        entry_low=99.0, entry_high=101.0, premium_sl=82.0, target1=127.0,
        target2=145.0, trailing_sl_rule="r", risk_reward=1.5, confidence=0.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=[], total=0.0),
    )
    card.hollow_reason = "setup: vwap_cross — reclaimed session VWAP"
    assert shadow_class(card) == "setup"

    with tempfile.TemporaryDirectory() as d:
        store = TradeStore(path=Path(d) / "p.json")
        t = store.create_from_signal(card, 1, 100.0, 65,
                                     notes="hollow: setup: vwap_cross — reclaimed")
        store.auto_close(t.id, 108.0, "target1")
        s = summarize(store)
        assert s["trades"] == 0, "setup fills must not touch clean aggregates"
        assert s["hollow"] is None and s["late_shadow"] is None
        blk = s["setup_shadow"]
        assert blk["trades"] == 1 and blk["net_pnl"] > 0
        assert blk["by_setup"]["vwap_cross"]["trades"] == 1
        row = next(r for r in s["rows"] if r["id"] == t.id)
        assert row["shadow_class"] == "setup" and row["setup"] == "vwap_cross"
    print("  LEDGER -> own block, per-setup split, no cross-class leakage")


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
