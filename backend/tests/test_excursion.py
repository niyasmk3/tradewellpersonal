"""Excursion tracking + target-curve tests.

This module's output is meant to SETTLE a strategy argument (should Target 1 be
+15% or +27%), so its arithmetic is hand-checked rather than recorded from a
run. A wrong curve here would replace one opinion with a confident wrong number.

Run:  python backend/tests/test_excursion.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.signals.models import Direction, TradingMode
from app.trades import excursion as exc
from app.trades import monitor
from app.trades.models import Trade, TradeStatus

NOW = int(time.time())


def mk(entry=100.0, exit_px=None, mfe=None, mae=None, status=TradeStatus.EXITED, **over):
    base = dict(
        id=f"T{id(over)}", symbol="NIFTY", mode=TradingMode.INTRADAY, direction=Direction.CE,
        contract="NIFTY 24350 CE", strike=24350, token=999,
        entry_premium=entry, lots=1, lot_size=75, quantity=75, initial_quantity=75,
        status=status, stop_loss=82.0, target1=127.0, target2=145.0, trailing_sl=82.0,
        created_at=NOW, entered_at=NOW, exited_at=NOW + 600, exit_premium=exit_px,
        mfe_premium=mfe, mae_premium=mae, excursion_from=over.pop('excursion_from', None),
    )
    base.update(over)
    return Trade(**base)


# --- the monitor must actually record it ------------------------------------

def test_monitor_records_both_extremes():
    t = mk(entry=100.0, exit_px=None, status=TradeStatus.ENTERED, exited_at=None)
    for px in (104.0, 96.0, 112.0, 91.0, 99.0):
        monitor.evaluate(t, current_premium=px, spot=24400.0, ist_minutes=600)
    assert t.mfe_premium == 112.0, t.mfe_premium
    assert t.mae_premium == 91.0, t.mae_premium
    assert t.mfe_at and t.mae_at
    print(f"  MONITOR-> MFE {t.mfe_premium} / MAE {t.mae_premium} tracked across 5 ticks")


def test_monitor_ignores_absent_premium():
    """A dead ticker must not register a 0 excursion."""
    t = mk(entry=100.0, exit_px=None, status=TradeStatus.ENTERED, exited_at=None)
    monitor.evaluate(t, current_premium=None, spot=24400.0, ist_minutes=600)
    assert t.mfe_premium is None and t.mae_premium is None
    print("  MONITOR-> no premium, no excursion recorded")


def test_excursion_stops_when_the_trade_closes():
    """THE property the whole analysis rests on: MFE is bounded by the trade's
    own life, so 'MFE >= X' means 'X was reachable BEFORE the exit'."""
    t = mk(entry=100.0, exit_px=None, status=TradeStatus.ENTERED, exited_at=None)
    monitor.evaluate(t, current_premium=105.0, spot=24400.0, ist_minutes=600)
    t.status = TradeStatus.EXITED
    monitor.evaluate(t, current_premium=200.0, spot=24400.0, ist_minutes=600)   # after the close
    assert t.mfe_premium == 105.0, t.mfe_premium
    print("  MONITOR-> post-exit moves are NOT counted (MFE stays 105)")


# --- the curve ---------------------------------------------------------------

def test_target_curve_hand_checked():
    """Three closed trades, entry 100 each:
         A: MFE 131 (+31%), stopped out at 94  -> realised -6%
         B: MFE 103 (+3%),  stopped out at 82  -> realised -18%
         C: MFE 118 (+18%), exited at 118      -> realised +18%
       At a +15% target: A and C reach it, B does not.
         expectancy = (15 + 15 + (-18)) / 3 = +4.00%
       At a +27% target: only A reaches it.
         expectancy = (27 + (-18) + 18) / 3 = +9.00%
       At a +40% target: none reach it.
         expectancy = (-6 + -18 + 18) / 3 = -2.00%
    """
    ts = [mk(entry=100.0, mfe=131.0, mae=94.0, exit_px=94.0),
          mk(entry=100.0, mfe=103.0, mae=82.0, exit_px=82.0),
          mk(entry=100.0, mfe=118.0, mae=97.0, exit_px=118.0)]
    out = exc.target_curve(ts)
    by = {c["target_pct"]: c for c in out["curve"]}
    assert out["trades"] == 3
    assert by[15.0]["reached"] == 2, by[15.0]
    assert by[15.0]["expectancy_pct"] == 4.00, by[15.0]
    assert by[27.0]["reached"] == 1, by[27.0]
    assert by[27.0]["expectancy_pct"] == 9.00, by[27.0]
    assert by[40.0]["reached"] == 0
    assert by[40.0]["expectancy_pct"] == -2.00, by[40.0]
    print(f"  CURVE  -> +15%: 2/3 reached, exp +4.00% | +27%: 1/3, exp +9.00% "
          f"| best {out['best_target_pct']}%")


def test_curve_prefers_the_higher_target_when_winners_run():
    """Guards the central trap: a lower target hits more often yet can still be
    worse, because it caps the trades that pay for the losers."""
    ts = [mk(entry=100.0, mfe=150.0, mae=95.0, exit_px=150.0) for _ in range(3)]
    ts.append(mk(entry=100.0, mfe=101.0, mae=80.0, exit_px=80.0))
    # Reaches +10% but never +50% — without this the two targets hit equally
    # often and the test asserts nothing about the trade-off.
    ts.append(mk(entry=100.0, mfe=120.0, mae=85.0, exit_px=85.0))
    out = exc.target_curve(ts)
    by = {c["target_pct"]: c for c in out["curve"]}
    assert by[50.0]["expectancy_pct"] > by[10.0]["expectancy_pct"], (by[10.0], by[50.0])
    assert by[10.0]["reached"] > by[50.0]["reached"]        # hits more often...
    print(f"  CURVE  -> +10% hits {by[10.0]['reached']}/4 for {by[10.0]['expectancy_pct']}%, "
          f"+50% hits {by[50.0]['reached']}/4 for {by[50.0]['expectancy_pct']}% — higher wins")


def test_rows_exclude_untracked_and_open_trades():
    """Rows predating the tracking have no MFE — they must be OMITTED, not
    counted as a zero excursion, which would drag every average toward 0."""
    ts = [mk(entry=100.0, mfe=None, mae=None, exit_px=94.0),                 # legacy
          mk(entry=100.0, mfe=120.0, mae=95.0, status=TradeStatus.ENTERED,   # still open
             exited_at=None, exit_px=None),
          mk(entry=100.0, mfe=120.0, mae=95.0, exit_px=120.0)]               # countable
    out = exc.target_curve(ts)
    assert out["trades"] == 1, out["trades"]
    print("  ROWS   -> legacy (no MFE) and open trades excluded from the curve")


def test_medians_and_empty_input():
    ts = [mk(entry=100.0, mfe=110.0, mae=95.0, exit_px=110.0),
          mk(entry=100.0, mfe=130.0, mae=70.0, exit_px=130.0),
          mk(entry=100.0, mfe=120.0, mae=88.0, exit_px=120.0)]
    out = exc.target_curve(ts)
    assert out["median_mfe_pct"] == 20.0, out["median_mfe_pct"]     # 10,20,30
    assert out["worst_mae_pct"] == -30.0, out["worst_mae_pct"]      # -30,-12,-5
    empty = exc.target_curve([])
    assert empty["trades"] == 0 and empty["best_target_pct"] is None
    print(f"  STATS  -> median MFE {out['median_mfe_pct']}%, worst MAE "
          f"{out['worst_mae_pct']}%; empty input safe")


def test_replay_todays_two_trades():
    """The real 21-Jul rows, measured earlier from Kite minute bars:
         #1 entry 25.80, stopped almost immediately -> MFE while open ~ +3%
         #2 entry 25.85, ran to 33.90 (+31.1%) then stopped at 24.30 (-6%)
       A 15% target catches #2 only — which is exactly what the tape showed."""
    ts = [mk(entry=25.80, mfe=26.55, mae=22.00, exit_px=22.05),
          mk(entry=25.85, mfe=33.90, mae=23.75, exit_px=24.30)]
    by = {c["target_pct"]: c for c in exc.target_curve(ts)["curve"]}
    assert by[15.0]["reached"] == 1, by[15.0]
    assert by[27.0]["reached"] == 1, by[27.0]
    assert by[40.0]["reached"] == 0
    print(f"  REPLAY -> +15% would have caught 1 of 2 (trade #2), matching the tape")


# --- partial measurements must be REJECTED, not averaged in ------------------

def test_partial_measurement_is_excluded_by_excursion_from():
    """The authoritative check: tracking that began after the entry did not see
    the whole trade, so the row cannot be trusted however plausible it looks."""
    late = mk(entry=100.0, mfe=120.0, mae=105.0, exit_px=118.0,
              excursion_from=NOW + 300)                 # started 5 min after entry
    ontime = mk(entry=100.0, mfe=120.0, mae=95.0, exit_px=118.0,
                excursion_from=NOW + 5)                 # started with the trade
    out = exc.target_curve([late, ontime])
    assert out["trades"] == 1, out["trades"]
    assert out["excluded_partial"] == 1, out["excluded_partial"]
    assert out["median_mae_pct"] == -5.0, out["median_mae_pct"]
    print(f"  PARTIAL-> late-started row excluded; {out['excluded_partial']} reported")


def test_legacy_row_with_impossible_mae_is_excluded():
    """The 21-Jul rows have no excursion_from. An MAE ABOVE the entry is proof
    the measurement started after the trade was already in profit."""
    bad = mk(entry=25.80, mfe=28.50, mae=27.75, exit_px=27.45)     # MAE +7.6% (!)
    good = mk(entry=29.37, mfe=31.00, mae=24.05, exit_px=23.95)    # MAE -18.1%
    out = exc.target_curve([bad, good])
    assert out["trades"] == 1, out["trades"]
    assert out["excluded_partial"] == 1
    assert out["median_mae_pct"] == -18.11, out["median_mae_pct"]
    print("  PARTIAL-> legacy row with a positive MAE excluded (impossible worst case)")


def test_the_actual_contaminated_output_is_fixed():
    """Reproduces the exact live numbers: three real rows, two of them partial,
    which together reported a median MAE of +6.77% — 'nothing went underwater'
    on a day everything got stopped."""
    ts = [mk(entry=28.70, mfe=31.00, mae=27.60, exit_px=29.95),   # MAE -3.8% ok
          mk(entry=25.85, mfe=30.60, mae=27.60, exit_px=29.95),   # MAE +6.8% BAD
          mk(entry=25.80, mfe=28.50, mae=27.75, exit_px=27.45)]   # MAE +7.6% BAD
    out = exc.target_curve(ts)
    assert out["trades"] == 1, out["trades"]
    assert out["excluded_partial"] == 2, out["excluded_partial"]
    assert out["median_mae_pct"] < 0, out["median_mae_pct"]
    print(f"  PARTIAL-> live case: 3 rows -> 1 kept, 2 excluded, "
          f"median MAE now {out['median_mae_pct']}% (was +6.77%)")


def test_a_genuine_never_underwater_winner_is_dropped_conservatively():
    """The heuristic's known cost, made explicit: a legacy row that ran up from
    the first tick has a legitimately positive MAE and is dropped. It is a
    WINNER, so dropping it biases the curve down — the safe direction."""
    winner = mk(entry=100.0, mfe=150.0, mae=100.5, exit_px=150.0)
    assert exc.target_curve([winner])["trades"] == 0
    # With excursion_from present the same row is correctly KEPT.
    tracked = mk(entry=100.0, mfe=150.0, mae=100.5, exit_px=150.0, excursion_from=NOW)
    assert exc.target_curve([tracked])["trades"] == 1
    print("  PARTIAL-> legacy winner dropped (conservative); tracked winner kept")


def test_monitor_stamps_excursion_from_once():
    t = mk(entry=100.0, exit_px=None, status=TradeStatus.ENTERED, exited_at=None)
    monitor.evaluate(t, current_premium=104.0, spot=24400.0, ist_minutes=600)
    first = t.excursion_from
    assert first is not None
    monitor.evaluate(t, current_premium=90.0, spot=24400.0, ist_minutes=600)
    assert t.excursion_from == first, "excursion_from moved; it must be set once"
    print("  MONITOR-> excursion_from stamped once and never overwritten")


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
