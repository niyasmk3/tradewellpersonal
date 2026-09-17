"""macd_aligned log field (17-Sep): stamp-only, never a gate.

Pins the helper's math (MACD(12,26,9) side-of-signal-line per direction, None
below 35 bars), and that the flag rides Trade rows like tape_state/golden.

Run:  python backend/tests/test_macd_log.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from app.signals.service import SignalService


def _df(closes):
    return pd.DataFrame({"close": closes})


def test_direction_and_math():
    svc = SignalService.__new__(SignalService)
    # 60 rising closes -> MACD above its signal line -> CE aligned, PE not.
    up = _df([100 + i for i in range(60)])
    assert svc._macd_aligned(up, "CE") is True
    assert svc._macd_aligned(up, "PE") is False
    dn = _df([200 - i for i in range(60)])
    assert svc._macd_aligned(dn, "PE") is True
    assert svc._macd_aligned(dn, "CE") is False
    print("  MATH   -> rising tape aligns CE, falling tape aligns PE")


def test_deceleration_reads_false():
    svc = SignalService.__new__(SignalService)
    # Strong rise then a long flat shelf: EMAs stay up but the MACD histogram
    # decays below its signal line — the fading-momentum read the sizing
    # found incremental. CE must read False here despite the uptrend.
    closes = [100 + i for i in range(40)] + [140.0] * 25
    assert svc._macd_aligned(_df(closes), "CE") is False
    print("  FADE   -> stalled momentum reads unaligned even in an uptrend")


def test_short_frame_is_none():
    svc = SignalService.__new__(SignalService)
    assert svc._macd_aligned(_df([100 + i for i in range(34)]), "CE") is None
    assert svc._macd_aligned(_df([]), "CE") is None
    print("  YOUNG  -> <35 bars yields None, never a guess")


def test_trade_row_carries_flag():
    from app.signals.models import Direction, TradingMode
    from app.trades.models import Trade, TradeStatus

    t = Trade(id="T1", signal_id="S1", symbol="NIFTY",
              mode=TradingMode.SCALP, direction=Direction.CE,
              contract="NIFTY 24500 CE", strike=24500.0,
              entry_premium=100.0, lots=1, lot_size=65, quantity=65,
              initial_quantity=65, status=TradeStatus.ENTERED,
              stop_loss=92.0, target1=106.0, target2=112.0,
              trailing_sl=92.0, created_at=1, entered_at=1,
              macd_aligned=True)
    assert Trade(**t.model_dump()).macd_aligned is True
    t2 = Trade(**{k: v for k, v in t.model_dump().items() if k != "macd_aligned"})
    assert t2.macd_aligned is None
    print("  LEDGER -> flag round-trips; legacy rows stay None")


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
