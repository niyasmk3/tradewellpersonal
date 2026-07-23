"""Signal-engine tests on synthetic data.

Runs as a plain script (no pytest needed):  python tests/test_signal_engine.py
Also pytest-compatible (functions named test_*).
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from app.config import get_settings
from app.market.indicators import compute_snapshot
from app.models.schemas import OptionChain, OptionRow
from app.signals.engine import SignalEngine
from app.signals.modes import build_profiles
from app.signals.models import Action, Bias, Direction, Regime, SignalState, TradingMode
from app.signals.store import SignalStore

BASE_TS = 1_700_000_000
STEP = 180
# These fixtures were tuned against a 70/60 gate and assert ENGINE LOGIC
# (bullish tape -> CE card), not the production threshold — which is deliberately
# retuned over time (raised to 78/70 on 2026-07-20). Pin the gate here so
# recalibrating the live engine never silently breaks these.
_CFG = get_settings().model_copy(update={"score_valid": 70, "score_wait": 60})
PROFILES = build_profiles(_CFG)


def make_df(kind: str, n: int = 80, start: float = 24_000.0) -> pd.DataFrame:
    rows = []
    price = start
    for i in range(n):
        drift = {"bull": 9.0, "bear": -9.0, "side": 0.0}[kind]
        osc = math.sin(i / 2.0) * (15.0 if kind == "side" else 2.5)
        close = price + drift + osc
        o = price
        hi = max(o, close) + 3
        lo = min(o, close) - 3
        rows.append({"ts": BASE_TS + i * STEP, "open": o, "high": hi, "low": lo, "close": close, "volume": 120_000})
        price = close
    return pd.DataFrame(rows)


def make_chain(spot: float, bias: str, symbol: str = "NIFTY", step: int = 50, depth: int = 10) -> OptionChain:
    atm = round(spot / step) * step
    rows = []
    for k in range(-depth, depth + 1):
        strike = atm + k * step
        # Realistic OI skew: call writing (resistance) builds above ATM, put
        # writing (support) below — so max-CE-OI sits above spot and max-PE-OI below.
        ce_oi = 500_000 + max(0, k) * 20_000
        pe_oi = 500_000 + max(0, -k) * 20_000
        ce_oic = 300_000 if (bias == "bear" and strike >= atm) else 0
        pe_oic = 300_000 if (bias == "bull" and strike <= atm) else 0
        rows.append(
            OptionRow(
                strike=strike,
                ce_token=1000 + k, ce_ltp=max(2.0, 100 - k * 8), ce_oi=ce_oi,
                ce_oi_change=ce_oic, ce_volume=5000,
                pe_token=2000 + k, pe_ltp=max(2.0, 100 + k * 8), pe_oi=pe_oi,
                pe_oi_change=pe_oic, pe_volume=5000,
            )
        )
    pcr = 1.25 if bias == "bull" else 0.75 if bias == "bear" else 1.0
    return OptionChain(symbol=symbol, expiry="2026-07-21", atm_strike=atm, pcr=pcr, rows=rows, updated_at=BASE_TS)


NOW_EVAL = BASE_TS + 80 * STEP


def _evaluate(kind: str, bias: str, mode: str = "intraday", ticks=None, cfg=None):
    df = make_df(kind)
    spot = float(df["close"].iloc[-1])
    engine = SignalEngine(cfg or _CFG)
    return engine.evaluate(
        profile=PROFILES[mode],
        symbol="NIFTY", df=df, ind=compute_snapshot(df), chain=make_chain(spot, bias),
        spot_ltp=spot, fut_ltp=spot, prev_close=float(df["close"].iloc[0]),
        day_high=float(df["high"].max()), day_low=float(df["low"].min()),
        vix_status="Stable", ticks=ticks, now=NOW_EVAL,
    )


def _all_option_ticks(price: float, ts=None, depth: int = 10) -> dict:
    """A tick for every chain token (whichever strike gets picked is covered)."""
    tick = {"last_price": price} if ts is None else {"last_price": price, "ts": ts}
    return {tok + k: dict(tick) for tok in (1000, 2000) for k in range(-depth, depth + 1)}


def test_bullish_generates_ce():
    r = _evaluate("bull", "bull")
    assert r.status.bias is Bias.BULLISH, r.status.regime
    assert r.action is Action.BUY_CE, (r.action, r.status.regime, r.status.bull_score)
    assert r.signal is not None and r.signal.direction is Direction.CE
    s = r.signal
    assert s.confidence >= 70
    assert s.premium_sl < s.entry_low < s.entry_high < s.target1 < s.target2
    assert "must stay above" in s.underlying_invalidation
    print(f"  BULL -> {s.contract} @ {s.entry_low}-{s.entry_high} SL {s.premium_sl} "
          f"T {s.target1}/{s.target2} conf {s.confidence} [{s.title}]")


def test_bearish_generates_pe():
    r = _evaluate("bear", "bear")
    assert r.status.bias is Bias.BEARISH, r.status.regime
    assert r.action is Action.BUY_PE, (r.action, r.status.regime, r.status.bear_score)
    assert r.signal is not None and r.signal.direction is Direction.PE
    assert "must stay below" in r.signal.underlying_invalidation
    print(f"  BEAR -> {r.signal.contract} conf {r.signal.confidence} [{r.signal.title}]")


def test_sideways_no_trade():
    r = _evaluate("side", "neutral")
    assert r.signal is None, r.signal
    assert r.action in (Action.AVOID, Action.WAIT), r.action
    assert r.status.regime in (Regime.SIDEWAYS, Regime.COMPRESSION, Regime.UNSAFE), r.status.regime
    print(f"  SIDE -> action={r.action.value} regime={r.status.regime.value} reason='{r.no_trade_reason}'")


def test_warming_up_with_few_candles():
    engine = SignalEngine(_CFG)
    df = make_df("bull", n=6)
    r = engine.evaluate(
        profile=PROFILES["intraday"],
        symbol="NIFTY", df=df, ind=compute_snapshot(df), chain=None,
        spot_ltp=24000, fut_ltp=24000, prev_close=23950, day_high=24100, day_low=23900,
        vix_status="Stable", ticks=None, now=BASE_TS,
    )
    assert r.status.regime is Regime.WARMING_UP
    assert r.signal is None
    print(f"  WARMUP -> regime={r.status.regime.value}")


def test_empty_and_single_candle_do_not_crash():
    """Regression (2026-07-20 live): right after the 09:15 open a single forming
    candle exists; the closed-candle trim produced an EMPTY frame and the engine
    — which scores both directions for the status display even while warming up
    — raised IndexError on df['close'].iloc[-1], freezing signals all session."""
    import pandas as pd

    engine = SignalEngine(_CFG)
    for df in (pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]),
               make_df("bull", n=1)):
        r = engine.evaluate(
            profile=PROFILES["intraday"],
            symbol="NIFTY", df=df, ind=compute_snapshot(df), chain=None,
            spot_ltp=24000, fut_ltp=24000, prev_close=23950, day_high=24100, day_low=23900,
            vix_status="Stable", ticks=None, now=BASE_TS,
        )
        assert r.status.regime is Regime.WARMING_UP, r.status.regime
        assert r.signal is None
    print("  EMPTY/1-BAR -> warming up, no crash")


def test_service_trim_keeps_last_candle():
    """The trim must never empty a one-candle frame (the crash's root cause)."""
    from app.market.candles import TIMEFRAME_SECONDS

    df = make_df("bull", n=1)
    secs = TIMEFRAME_SECONDS["3m"]
    now = int(df["ts"].iloc[-1]) + 1          # candle still forming
    trimmed = df.iloc[:-1] if len(df) > 1 and int(df["ts"].iloc[-1]) + secs > now else df
    assert len(trimmed) == 1, "single forming candle must survive the trim"
    print("  TRIM -> 1-candle frame preserved")


def test_positional_mode_differs_from_intraday():
    r = _evaluate("bull", "bull", mode="positional")
    assert r.mode is TradingMode.POSITIONAL
    assert r.status.mode is TradingMode.POSITIONAL
    assert r.action is Action.BUY_CE, (r.action, r.status.bull_score)
    s = r.signal
    assert s is not None and s.mode is TradingMode.POSITIONAL
    # Positional risk profile: wider premium stop (~30%) and bigger R:R than intraday.
    assert s.risk_reward == PROFILES["positional"].rr_target1
    assert s.premium_sl < s.ref_entry_premium * 0.75  # ~30% stop → SL well below entry
    # Validity window is far longer than intraday's 8 minutes.
    assert (s.valid_until - s.created_at) > 3600
    print(f"  POS  -> {s.contract} SL {s.premium_sl} T {s.target1}/{s.target2} "
          f"R:R 1:{s.risk_reward} valid {(s.valid_until - s.created_at) // 3600}h")


# --- premium freshness gate, through the full evaluate() path ----------------
# The 21/22-Jul incident: 5 of 9 cards priced their zones on premiums 3 min to
# a DAY stale. These pin the whole pipeline, not just the premium_quote helper.

def test_stale_quote_blocks_issue():
    r = _evaluate("bear", "bear", ticks=_all_option_ticks(100.0, ts=NOW_EVAL - 3600))
    assert r.signal is None, r.signal
    assert r.action is Action.AVOID
    assert "3600s old" in (r.no_trade_reason or ""), r.no_trade_reason
    print(f"  GATE -> hour-old quote refused: '{r.no_trade_reason}'")


def test_unverifiable_quote_blocks_issue():
    """No tick at all, and a tick with no exchange stamp, are both refusals —
    an age nothing can verify is not an age of zero."""
    for ticks in ({}, _all_option_ticks(100.0, ts=None)):
        r = _evaluate("bear", "bear", ticks=ticks)
        assert r.signal is None, r.signal
        assert "No timestamped quote" in (r.no_trade_reason or ""), r.no_trade_reason
    print("  GATE -> missing/unstamped quotes refused as unverifiable")


def test_fresh_tick_reprices_the_whole_plan():
    """A fresh tick must not just pass the gate — it must BE the price. The
    chain says ~₹100 for the picked strike; the tape says ₹91. Zone, and
    therefore SL and targets, must anchor to ₹91."""
    r = _evaluate("bear", "bear", ticks=_all_option_ticks(91.0, ts=NOW_EVAL - 5))
    s = r.signal
    assert s is not None, r.no_trade_reason
    assert s.ref_entry_premium == 91.0, s.ref_entry_premium
    assert s.entry_low <= 91.0 <= s.entry_high, (s.entry_low, s.entry_high)
    print(f"  GATE -> tick ₹91 repriced the plan: zone {s.entry_low}-{s.entry_high}")


def test_gate_disabled_accepts_ancient_ticks():
    """SIGNAL_MAX_PREMIUM_AGE_S=0 is the offline-replay escape hatch."""
    cfg = _CFG.model_copy(update={"signal_max_premium_age_s": 0})
    r = _evaluate("bear", "bear", cfg=cfg,
                  ticks=_all_option_ticks(91.0, ts=NOW_EVAL - 100_000))
    assert r.signal is not None, r.no_trade_reason
    assert r.signal.ref_entry_premium == 91.0     # price override still applies
    print("  GATE -> disabled gate accepts a day-old tick (replay mode)")


def test_store_stabilises_and_cancels():
    store = SignalStore()
    bull = _evaluate("bull", "bull")
    a = store.reconcile(bull, now=BASE_TS + 100)
    assert a.signal is not None
    first_id = a.signal.id

    # Same direction re-eval a moment later -> keep the SAME signal (no churn).
    bull2 = _evaluate("bull", "bull")
    bull2.evaluated_at = BASE_TS + 160
    b = store.reconcile(bull2, now=BASE_TS + 160)
    assert b.signal is not None and b.signal.id == first_id, "signal should be stable"

    # Trend flips bearish -> active signal cancelled.
    bear = _evaluate("bear", "bear")
    c = store.reconcile(bear, now=BASE_TS + 220)
    hist = store.history("NIFTY", TradingMode.INTRADAY)
    assert any(x.state is SignalState.CANCELLED for x in hist), [x.state for x in hist]
    print(f"  STORE -> stable id kept, then cancelled on flip; history={len(hist)}")


def _main():
    tests = [
        test_bullish_generates_ce,
        test_bearish_generates_pe,
        test_sideways_no_trade,
        test_warming_up_with_few_candles,
        test_empty_and_single_candle_do_not_crash,
        test_service_trim_keeps_last_candle,
        test_positional_mode_differs_from_intraday,
        test_stale_quote_blocks_issue,
        test_unverifiable_quote_blocks_issue,
        test_fresh_tick_reprices_the_whole_plan,
        test_gate_disabled_accepts_ancient_ticks,
        test_store_stabilises_and_cancels,
    ]
    failed = 0
    for t in tests:
        try:
            print(f"• {t.__name__}")
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
