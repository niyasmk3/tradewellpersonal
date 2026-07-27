"""Market-pulse computation tests — synthetic frames, hand-checked numbers.

Run:  python backend/tests/test_pulse.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time

from app.market.candles import CandleEngine
from app.market.pulse import _pcr_first, compute_pulse
from app.models.schemas import OptionChain
from app.state import MarketState, UnderlyingMeta

FUT, VIX = 500, 900


def _state() -> MarketState:
    s = MarketState()
    s.underlyings["NIFTY"] = UnderlyingMeta(
        symbol="NIFTY", spot_token=1, spot_tradingsymbol="NIFTY 50",
        strike_step=50, fut_token=FUT, fut_tradingsymbol="NIFTYFUT",
    )
    eng = CandleEngine(FUT)
    s.candle_engines[FUT] = eng
    now = int(time.time())
    ist_midnight = (now + 19800) // 86400 * 86400 - 19800
    open_ts = ist_midnight + int(9.25 * 3600)          # 09:15 IST today

    # Two PRIOR sessions on the multi-day 15m frame: constant volume 1000/bar,
    # daily range exactly 100 points (24000-24100).
    rows15 = []
    for d in (2, 1):
        day_open = open_ts - d * 86400
        for i in range(25):
            rows15.append({"ts": day_open + i * 900, "open": 24050, "high": 24100 if i == 0 else 24060,
                           "low": 24000 if i == 0 else 24040, "close": 24050, "volume": 1000})
    # Today on the same frame: 5 bars minus forming, volume 2000/bar -> 2x run-rate.
    for i in range(5):
        rows15.append({"ts": open_ts + i * 900, "open": 24050, "high": 24060,
                       "low": 24040, "close": 24050, "volume": 2000})
    eng.seed("15m", rows15)

    # Today's session (3m): range 23990-24040, last close 24030 -> 80% of range.
    rows3 = []
    for i in range(20):
        px = 23990 + i * 2.5
        rows3.append({"ts": open_ts + i * 180, "open": px, "high": px + 6,
                      "low": px - 4, "close": px + 2, "volume": 500})
    eng.seed("3m", rows3)

    s.ticks[FUT] = {"last_price": 24030.0}
    s.vix_token = VIX
    s.ticks[VIX] = {"last_price": 14.0}
    s.vix_daily_closes = [13.0, 13.5]                  # prev close 13.5 -> +3.7%
    s.set_option_chain("NIFTY:nearest", OptionChain(
        symbol="NIFTY", expiry=None, atm_strike=24050.0, pcr=0.90, rows=[],
        updated_at=now))
    return s


def test_pulse_core_numbers():
    _pcr_first.clear()
    s = _state()
    p = compute_pulse(s, "NIFTY")

    # Range position: (24030 - low) / (high - low) using the 3m frame extremes.
    lo, hi = p["day_low"], p["day_high"]
    assert abs(p["range_pos_pct"] - (24030 - lo) / (hi - lo) * 100) < 0.2
    assert p["range_pos_pct"] > 60, p["range_pos_pct"]

    # Volume run-rate: 2000/bar today vs 1000/bar prior = 2.0 (forming bar dropped).
    assert abs(p["vol_run_rate"] - 2.0) < 0.01, p["vol_run_rate"]

    # Range vs typical: today's 3m-frame range over the 100-pt prior daily mean.
    assert abs(p["range_vs_typical_pct"] - (hi - lo)) < 1.0, p["range_vs_typical_pct"]

    # VWAP stretch present and finite; ATR from the same frame.
    assert p["vwap"] and p["atr"] and isinstance(p["vwap_dist_atr"], float)

    # VIX vs last daily close.
    assert abs(p["vix_chg_pct"] - (14.0 - 13.5) / 13.5 * 100) < 0.05

    print("  PULSE  -> range %.0f%%, run-rate %.2fx, range-used %.0f%%, VIX %+0.1f%%"
          % (p["range_pos_pct"], p["vol_run_rate"], p["range_vs_typical_pct"], p["vix_chg_pct"]))


def test_pcr_shift_anchors_to_first_observation():
    _pcr_first.clear()
    s = _state()
    p1 = compute_pulse(s, "NIFTY")
    assert p1["pcr"] == 0.90 and p1["pcr_shift"] == 0.0
    # PCR drifts; the anchor must stay at the day's first observation.
    s.set_option_chain("NIFTY:nearest", OptionChain(
        symbol="NIFTY", expiry=None, atm_strike=24050.0, pcr=1.02, rows=[],
        updated_at=int(time.time())))
    p2 = compute_pulse(s, "NIFTY")
    assert p2["pcr"] == 1.02 and abs(p2["pcr_shift"] - 0.12) < 0.001
    assert p2["pcr_first"] == 0.90
    print("  PCRDRF -> anchored at first observation (0.90), drift +0.12")


def test_cold_state_returns_nulls_not_errors():
    _pcr_first.clear()
    p = compute_pulse(MarketState(), "NIFTY")
    assert p["symbol"] == "NIFTY" and "updated_at" in p
    for k in ("range_pos_pct", "vol_run_rate", "pcr", "vix"):
        assert k not in p, f"{k} must be absent when inputs are cold"
    print("  COLD   -> empty state yields honest absence, no exceptions")


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
