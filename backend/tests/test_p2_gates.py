"""P2 signal-quality pieces that aren't engine-path: PCR de-dup, T1
calibration guards, the events calendar, and the score component that reads
them. The engine-path gates (extension, invalidation room) live in
test_signal_engine; the session/post-gap vetoes are service-state logic pinned
here through their pure parts.

Run:  python backend/tests/test_p2_gates.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.market import events
from app.signals import calibration
from app.signals.features import OiAnalysis
from app.signals.models import Direction, TradingMode
from app.signals.scoring import _options

# ---- PCR double-count (the 22-Jul 09:15 card carried PCR twice) -------------


def _oi(bias, source, pcr):
    return OiAnalysis(pcr=pcr, resistance_strike=24200.0, support_strike=23800.0,
                      put_writing=0.0, call_writing=0.0, bias=bias,
                      notes=[f"PCR {pcr}"], bias_source=source)


def test_pcr_not_paid_twice_when_it_is_the_bias():
    doubled = _options(_oi("bearish", "pcr", 0.82), bullish=False, spot=24000.0)
    honest = _options(_oi("bearish", "oi", 0.82), bullish=False, spot=24000.0)
    # Same directional agreement; the pcr-sourced bias must NOT also collect
    # the +5 alignment bonus the oi-sourced one legitimately earns.
    assert honest.points == doubled.points + 5, (honest.points, doubled.points)
    print(f"  PCR    -> oi-bias {honest.points} vs pcr-bias {doubled.points}: no double pay")


def test_pcr_alignment_still_paid_against_a_neutral_bias():
    neutral = _options(_oi("neutral", "none", 0.82), bullish=False, spot=24000.0)
    # bias neutral +4, pcr aligned +5, walls favourable +5 = 14
    assert neutral.points == 14.0, neutral.points
    print("  PCR    -> neutral OI bias still earns the alignment bonus")


# ---- T1 calibration guards ---------------------------------------------------


class _T:
    def __init__(self, mfe_pct, mode="intraday", status="exited", gap=0):
        from app.paper.service import HONEST_FILLS_FROM

        self.mode = type("M", (), {"value": mode})()
        self.status = type("S", (), {"value": status})()
        self.entry_premium = 100.0
        self.mfe_premium = 100.0 * (1 + mfe_pct)
        # Inside the honest-fill era — pre-era rows are rejected wholesale
        # (that rejection has its own test in test_p3_p5).
        self.entered_at = HONEST_FILLS_FROM + 3600
        self.excursion_from = self.entered_at + gap


def test_calibration_needs_samples_and_clamps():
    assert calibration._p75_mfe_pct([_T(0.10)] * 29) is None      # < 30
    p75 = calibration._p75_mfe_pct([_T(0.05)] * 15 + [_T(0.10)] * 15 + [_T(0.20)] * 10)
    assert p75 is not None and 0.10 <= p75 <= 0.20, p75
    dirty = [_T(0.10, gap=120)] * 40                               # late tracking
    assert calibration._p75_mfe_pct(dirty) is None
    posn = [_T(0.10, mode="positional")] * 40                      # wrong mode
    assert calibration._p75_mfe_pct(posn) is None
    print(f"  CALIB  -> 30-sample floor, clean-tracking filter; P75={p75:.3f}")


# ---- events calendar ---------------------------------------------------------


def _with_calendar(entries, fn):
    import pathlib
    tmp = pathlib.Path("/tmp/tw-events-test.json")
    tmp.write_text(json.dumps(entries))
    real_path, real_cache = events._PATH, dict(events._cache)
    events._PATH = tmp
    events._cache = {"mtime": None, "events": []}
    try:
        return fn()
    finally:
        events._PATH = real_path
        events._cache = real_cache
        tmp.unlink(missing_ok=True)


def test_event_window_and_quiet_days():
    now = int(time.time())
    ist = time.gmtime(now + 19800)
    today = f"{ist.tm_year:04d}-{ist.tm_mon:02d}-{ist.tm_mday:02d}"
    in_2h = time.gmtime(now + 19800 + 7200)
    # Date taken from the +2h moment itself, so a run near IST midnight builds
    # tomorrow's date instead of a same-day 01:00 that reads as -22h.
    soon_date = f"{in_2h.tm_year:04d}-{in_2h.tm_mon:02d}-{in_2h.tm_mday:02d}"
    soon = f"{in_2h.tm_hour:02d}:{in_2h.tm_min:02d}"

    if soon_date == today:
        note = _with_calendar(
            [{"date": today, "time": soon, "label": "RBI policy"}],
            lambda: events.upcoming(now))
        assert note and "RBI policy" in note, note
    else:
        note = "skipped near IST midnight"

    allday = _with_calendar([{"date": today, "label": "Budget"}],
                            lambda: events.upcoming(now))
    assert allday == "Budget today"

    faraway = _with_calendar([{"date": "2099-01-01", "label": "x"}],
                             lambda: events.upcoming(now))
    assert faraway is None

    # Same-day event 8 hours away must NOT halve sizing all morning.
    if ist.tm_hour < 10:                     # only meaningful when it fits the day
        far_t = time.gmtime(now + 19800 + 8 * 3600)
        distant = _with_calendar(
            [{"date": today, "time": f"{far_t.tm_hour:02d}:{far_t.tm_min:02d}", "label": "x"}],
            lambda: events.upcoming(now))
        assert distant is None
    print(f"  EVENTS -> window works: '{note}' / '{allday}' / far-future quiet")


def test_broken_calendar_is_silent():
    import pathlib
    tmp = pathlib.Path("/tmp/tw-events-test.json")
    tmp.write_text("{not json")
    real_path, real_cache = events._PATH, dict(events._cache)
    events._PATH = tmp
    events._cache = {"mtime": None, "events": []}
    try:
        assert events.upcoming(int(time.time())) is None
    finally:
        events._PATH = real_path
        events._cache = real_cache
        tmp.unlink(missing_ok=True)
    print("  EVENTS -> malformed calendar ignored, never raises")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
