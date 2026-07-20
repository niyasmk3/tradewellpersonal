"""Phase 5 backtest-engine tests (synthetic candles — no Kite needed).

Run:  python backend/tests/test_backtest.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from app.backtest import engine
from app.backtest.models import BacktestTrade
from app.signals.models import Direction, TradingMode
from app.signals.modes import ModeProfile

# A day bucket aligned to IST 09:15 so every synthetic bar shares one session.
_DAY0 = 19676 * 86400 + 13500  # 09:15 IST on some day
_BAR = 180                     # 3-minute bars


def _profile(mode=TradingMode.INTRADAY, timeframe="3m", score_valid=70,
             rr=1.5, min_candles=15):
    return ModeProfile(
        mode=mode, label="Test", timeframe=timeframe, expiry_key="nearest",
        validity_seconds=480, score_valid=score_valid, score_wait=60,
        premium_sl_pct=0.18, rr_target1=rr, rr_target2=2.5, strike_bias="atm_otm",
        min_candles=min_candles, horizon="test",
    )


def _trend_day(n=60, start=20000.0, step=10.0, up=True, base_ts=_DAY0):
    """A clean single-session trend that should score tradeable."""
    rows = []
    price = start
    for i in range(n):
        o = price
        c = price + step if up else price - step
        hi = max(o, c) + 2
        lo = min(o, c) - 2
        rows.append({"ts": base_ts + i * _BAR, "open": o, "high": hi,
                     "low": lo, "close": c, "volume": 100_000})
        price = c
    return pd.DataFrame(rows)


def test_uptrend_generates_winning_ce():
    # score_valid=65: a clean ramp scores ~67 (RSI is undefined with zero
    # down-moves, and breakout can't clear the current bar's own high). The exact
    # 70 gate is covered in test_signal_engine.py; here we exercise engine mechanics.
    df = _trend_day(up=True)
    res = engine.run(df, _profile(score_valid=65), "NIFTY")
    assert res.trades_total >= 1, "a clean uptrend should produce at least one signal"
    assert res.ce_trades == res.trades_total, "uptrend signals must all be CE"
    assert res.pe_trades == 0
    assert res.expectancy_r > 0, f"uptrend expectancy should be positive, got {res.expectancy_r}"
    assert res.win_rate >= 60, f"uptrend win-rate low: {res.win_rate}"
    # every trade is closed with a valid outcome (no dangling position)
    assert all(t.outcome in {"target", "stop", "eod", "time"} for t in res.trades)


def test_downtrend_generates_pe():
    df = _trend_day(up=False)
    res = engine.run(df, _profile(score_valid=65), "NIFTY")
    assert res.trades_total >= 1
    assert res.pe_trades == res.trades_total, "downtrend signals must all be PE"
    assert res.ce_trades == 0
    assert res.expectancy_r > 0


def test_no_overnight_hold_intraday():
    """Two trending sessions back to back — intraday must never carry a trade across days."""
    d1 = _trend_day(up=True, base_ts=_DAY0)
    d2 = _trend_day(up=True, start=20800.0, base_ts=_DAY0 + 86400)
    df = pd.concat([d1, d2], ignore_index=True)
    res = engine.run(df, _profile(score_valid=65), "NIFTY")
    assert res.trades_total >= 1, "expected trades across the two sessions"
    for t in res.trades:
        # entry and exit must fall on the same IST day bucket
        assert engine._day(t.entry_ts) == engine._day(t.exit_ts), "intraday trade crossed a session"


def test_summarize_math():
    trades = [
        BacktestTrade(entry_ts=1, exit_ts=2, direction=Direction.CE, regime="strong_bullish",
                      score=80, entry=100, stop=95, target=110, exit=110, r_multiple=2.0, outcome="target"),
        BacktestTrade(entry_ts=3, exit_ts=4, direction=Direction.CE, regime="moderate_bullish",
                      score=72, entry=100, stop=95, target=110, exit=95, r_multiple=-1.0, outcome="stop"),
        BacktestTrade(entry_ts=5, exit_ts=6, direction=Direction.PE, regime="strong_bearish",
                      score=78, entry=100, stop=105, target=90, exit=90, r_multiple=2.0, outcome="target"),
    ]
    df = _trend_day(n=20)
    res = engine._summarize(df, _profile(), "NIFTY", trades)
    assert res.trades_total == 3
    assert res.wins == 2 and res.losses == 1
    assert res.win_rate == round(100 * 2 / 3, 1)
    assert res.total_r == 3.0
    assert res.expectancy_r == 1.0
    assert res.avg_win_r == 2.0
    assert res.avg_loss_r == -1.0
    assert res.profit_factor == 4.0            # (2+2) / |−1|
    assert res.equity_curve == [2.0, 1.0, 3.0]
    assert res.max_drawdown_r == 1.0           # 2.0 peak -> 1.0 trough
    assert res.ce_trades == 2 and res.pe_trades == 1
    assert res.ce_win_rate == 50.0 and res.pe_win_rate == 100.0


def test_day_context_uses_prior_session_only():
    # two sessions: closes end at 101 (day D) and 201 (day D+1)
    days = [1000, 1000, 1001, 1001]
    closes = [100.0, 101.0, 200.0, 201.0]
    vix_by_day = {1000: 25.0}          # High on day D
    prev_close, vix_status = engine._day_context(days, closes, vix_by_day)
    # day D has no prior session; day D+1 inherits D's last close + D's VIX
    assert prev_close[1000] is None
    assert prev_close[1001] == 101.0
    assert vix_status[1000] is None    # no prior day → no VIX (no look-ahead)
    assert vix_status[1001] == "High"  # 25.0 -> High bucket


def test_vix_threading_runs():
    d1 = _trend_day(up=True, base_ts=_DAY0)
    d2 = _trend_day(up=True, start=20800.0, base_ts=_DAY0 + 86400)
    df = pd.concat([d1, d2], ignore_index=True)
    # prior-day VIX for day 2 comes from day 1's bucket; must not crash and must
    # still return a well-formed result.
    day1 = engine._day(int(d1["ts"].iloc[0]))
    res = engine.run(df, _profile(score_valid=65), "NIFTY", vix_by_day={day1: 11.0})
    assert res.bars == len(df)
    assert res.trades_total >= 0


def test_empty_and_short_no_crash():
    empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    res = engine.run(empty, _profile(), "NIFTY")
    assert res.trades_total == 0 and res.bars == 0

    short = _trend_day(n=8)                     # below warmup
    res2 = engine.run(short, _profile(), "NIFTY")
    assert res2.trades_total == 0


def test_all_wins_no_profit_factor():
    """profit_factor is None when there are no losing trades (no divide-by-zero)."""
    trades = [BacktestTrade(entry_ts=1, exit_ts=2, direction=Direction.CE, regime="x",
                            score=80, entry=100, stop=95, target=110, exit=110,
                            r_multiple=1.5, outcome="target")]
    res = engine._summarize(_trend_day(n=20), _profile(), "NIFTY", trades)
    assert res.profit_factor is None
    assert res.losses == 0 and res.avg_loss_r == 0.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)
