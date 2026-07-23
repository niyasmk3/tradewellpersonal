"""Score-history store tests.

The store feeds a diagnostics sparkline from inside the LIVE evaluation loop,
so the properties that matter are: bounded memory, same-day-only series, the
newest point always surviving decimation, and record() never raising.

Run:  python backend/tests/test_score_history.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.signals.score_history import ScoreHistoryStore

# 2026-07-23 09:15:00 IST
T0 = 1_784_778_300


def _fill(s, n, start=T0, step=5):
    for i in range(n):
        s.record("NIFTY", "intraday", start + i * step,
                 bull=40.0 + i % 10, bear=60.0 - i % 10,
                 direction="PE", components={"Trend & momentum": float(i % 20)})


def test_records_and_serves_oldest_first():
    s = ScoreHistoryStore()
    _fill(s, 10)
    out = s.series("NIFTY", "intraday")
    assert out["count"] == 10
    ts = [p["ts"] for p in out["points"]]
    assert ts == sorted(ts)
    assert out["points"][-1]["components"]["Trend & momentum"] == 9.0
    print("  SCORE  -> 10 evaluations recorded, served oldest-first")


def test_same_second_overwrites_not_duplicates():
    s = ScoreHistoryStore()
    s.record("NIFTY", "intraday", T0, 40, 60, "PE", {"x": 1.0})
    s.record("NIFTY", "intraday", T0, 41, 59, "PE", {"x": 2.0})
    out = s.series("NIFTY", "intraday")
    assert out["count"] == 1 and out["points"][0]["components"]["x"] == 2.0
    print("  SCORE  -> same-second re-record overwrites, no duplicate points")


def test_new_ist_day_clears_yesterday():
    """A trend line across a market close would be fiction."""
    s = ScoreHistoryStore()
    _fill(s, 50)
    s.record("NIFTY", "intraday", T0 + 86_400, 50, 50, "CE", {})
    out = s.series("NIFTY", "intraday", minutes=0)
    assert out["count"] == 1, out["count"]
    print("  SCORE  -> first record of a new IST day clears the old series")


def test_decimation_keeps_the_newest_point():
    s = ScoreHistoryStore()
    _fill(s, 1000)
    out = s.series("NIFTY", "intraday", minutes=480, max_points=100)
    assert out["count"] <= 101
    assert out["points"][-1]["ts"] == T0 + 999 * 5, "endpoint must be the newest"
    print(f"  SCORE  -> 1000 points decimated to {out['count']}, newest kept")


def test_minutes_window_filters_old_points():
    s = ScoreHistoryStore()
    _fill(s, 1000)                                  # spans ~83 minutes
    out = s.series("NIFTY", "intraday", minutes=10)
    assert all(p["ts"] >= T0 + 999 * 5 - 600 for p in out["points"])
    assert out["count"] > 0
    print(f"  SCORE  -> 10-minute window returns {out['count']} recent points")


def test_bounded_memory_and_unknown_key():
    s = ScoreHistoryStore(maxlen=50)
    _fill(s, 500)
    assert s.series("NIFTY", "intraday", minutes=0)["count"] == 50
    assert s.series("BANKNIFTY", "intraday")["count"] == 0
    print("  SCORE  -> deque bounded at maxlen; unknown key serves empty")


def test_record_never_raises():
    """Called from the live loop — a diagnostics feature must not stop it."""
    s = ScoreHistoryStore()
    s.record("NIFTY", "intraday", "not-a-ts", 1, 2, None, None)  # type: ignore[arg-type]
    s.record("NIFTY", "intraday", T0, 40, 60, None, None)
    assert s.series("NIFTY", "intraday")["count"] >= 1
    print("  SCORE  -> garbage input swallowed; live loop unaffected")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
