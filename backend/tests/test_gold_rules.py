"""Gold rules: the one simulate_day code path, its tie-breaks, and the
backtest/forward partition.

What is pinned here IS the pre-registration contract: entries at bar closes,
stop wins a shared bar, time exit at the deadline's last close, H1 targeting
the gap origin, and the freeze-date phase split (freeze day itself = backtest,
because a partially-observed day cannot be blind).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime

import pandas as pd

from app.gold import rules
from app.gold.live import states_of, transitions
from app.gold.rules import IST, simulate_day


def _ts(h, m, day=20):
    return int(datetime(2026, 8, day, h, m, tzinfo=IST).timestamp())


def _bar(h, m, o, hi, lo, c, day=20):
    return {"ts": _ts(h, m, day), "open": o, "high": hi, "low": lo, "close": c}


def _by_rule(cards):
    return {c["rule"]: c for c in cards}


def _flat_morning(px=100000.0):
    """09:00–09:30 with no qualifying move anywhere."""
    return [_bar(9, 3 * i, px, px + 10, px - 10, px) for i in range(11)]


def test_h2_follows_burst_to_target():
    bars = [
        _bar(9, 0, 100000, 100120, 99990, 100100),
        _bar(9, 24, 100100, 100210, 100090, 100200),
        _bar(9, 27, 100200, 100210, 100150, 100200),   # closes 09:30 — the print AND the entry
        _bar(9, 30, 100200, 100600, 100150, 100550),   # high takes the +0.30% target
    ]
    c = _by_rule(simulate_day(bars, prev_close=100000.0))["H2"]
    assert c["state"] == "closed" and c["qualified"]
    assert c["direction"] == "LONG" and c["entry_px"] == 100200
    assert c["exit_reason"] == "target"
    assert abs(c["target_px"] - 100200 * 1.003) < 0.01
    assert c["net_rs"] == round((c["exit_px"] - 100200) * 10 - 250, 0)


def test_stop_wins_when_both_touched_in_one_bar():
    bars = [
        _bar(9, 0, 100000, 100120, 99990, 100100),
        _bar(9, 27, 100200, 100210, 100150, 100200),
        # One wide bar touches BOTH the stop (99949.5) and the target (100500.6).
        _bar(9, 30, 100200, 100700, 99900, 100400),
    ]
    c = _by_rule(simulate_day(bars, prev_close=100000.0))["H2"]
    assert c["exit_reason"] == "stop"
    assert abs(c["exit_px"] - 100200 * 0.9975) < 0.01
    assert c["net_rs"] < 0


def test_h1_fades_gap_to_previous_close():
    prev = 100000.0
    bars = [
        _bar(9, 0, 100600, 100650, 100500, 100560),    # +0.6% gap — qualifies
        _bar(9, 12, 100560, 100600, 100500, 100550),   # closes 09:15 — the entry
        _bar(9, 15, 100550, 100560, 99990, 100050),    # low tags the gap origin
    ]
    c = _by_rule(simulate_day(bars, prev_close=prev))["H1"]
    assert c["qualified"] and c["direction"] == "SHORT"
    assert c["entry_px"] == 100550
    assert c["target_px"] == prev                      # gap fill, not a % target
    assert c["exit_reason"] == "target" and c["exit_px"] == prev
    assert c["points"] == 550.0 and c["net_rs"] == 550 * 10 - 250


def test_time_exit_at_deadlines_last_close():
    bars = _flat_morning() + [
        _bar(16, 57, 100000, 100010, 99990, 100000),   # closes 17:00 — window start
        _bar(17, 57, 100100, 100160, 100090, 100150),  # closes 18:00 — +0.15%, entry
        _bar(19, 0, 100150, 100200, 100100, 100180),   # never touches either level
        _bar(20, 54, 100180, 100200, 100100, 100120),  # closes 20:57 — deadline's last close
        _bar(21, 0, 100120, 100900, 100100, 100800),   # beyond 21:00 — must be ignored
    ]
    c = _by_rule(simulate_day(bars, prev_close=100000.0))["H3"]
    assert c["state"] == "closed" and c["exit_reason"] == "time"
    assert c["exit_px"] == 100120 and c["exit_ts"] == _ts(20, 54)


def test_h1_stands_aside_when_gap_filled_before_entry():
    """Review catch: a qualifying gap that retraces past the previous close
    before the 09:15 entry leaves the gap-fill 'target' at/behind the entry —
    any later touch would book a LOSS labelled 'target'. The fade is over;
    the day is a stand-aside, live and backtest alike."""
    prev = 100000.0
    bars = [
        _bar(9, 0, 100600, 100650, 100500, 100560),    # +0.6% gap — qualifies
        _bar(9, 12, 100560, 100600, 99900, 99950),     # gap fills BEFORE entry
        _bar(9, 15, 99950, 100650, 99900, 100600),     # would tag prev close for a loss
    ]
    c = _by_rule(simulate_day(bars, prev_close=prev))["H1"]
    assert c["state"] == "no_setup" and c["qualified"]
    assert "no fade left" in c["note"]
    c = _by_rule(simulate_day(bars, prev_close=prev, live=True))["H1"]
    assert c["state"] == "no_setup"


def test_live_deadline_with_no_window_bars_is_no_data():
    """Review catch: entry bar exists, zero later bars closed inside the exit
    window, deadline passed — the live card must render the backtest's data-
    hole verdict, not sit 'open' forever with nothing able to close it."""
    bars = [
        _bar(9, 0, 100000, 100120, 99990, 100100),
        _bar(9, 27, 100200, 100210, 100150, 100200),   # entry
        _bar(15, 0, 100200, 100260, 100160, 100240),   # first later bar closes PAST 15:00
    ]
    for is_live in (False, True):
        c = _by_rule(simulate_day(bars, 100000.0, live=is_live))["H2"]
        assert c["state"] == "no_data", (is_live, c["state"])


def test_split_live_bars_drops_forming_bar_and_reads_prev_close_from_fetch():
    """Review catches (both HIGH): Kite's last candle is the forming one —
    scoring it let live cards qualify/enter minutes before the print the
    ledger grades — and prev_close must come from the fetch itself, not a
    store that only advances on a manual sync."""
    from datetime import date

    from app.gold import service

    def kbar(day, h, m, close):
        return {"date": datetime(2026, 9, day, h, m, tzinfo=IST),
                "open": close, "high": close, "low": close, "close": close}

    raw = [
        kbar(2, 14, 0, 101000.0),     # a mid-session print — must NOT win
        kbar(2, 23, 27, 100000.0),    # the previous session's true last close
        kbar(3, 9, 0, 100450.0),      # closed (09:03 <= now)
        kbar(3, 9, 3, 100500.0),      # forming at 09:05 — must be dropped
    ]
    now_epoch = int(datetime(2026, 9, 3, 9, 5, tzinfo=IST).timestamp())
    bars, prev_close = service.split_live_bars(raw, now_epoch, date(2026, 9, 3))
    assert prev_close == 100000.0
    assert [b["close"] for b in bars] == [100450.0]


def test_below_threshold_is_no_setup():
    bars = [
        _bar(9, 0, 100000, 100060, 99990, 100050),
        _bar(9, 27, 100050, 100110, 100040, 100100),   # +0.10% < the 0.15% gate
    ]
    c = _by_rule(simulate_day(bars, prev_close=100000.0))["H2"]
    assert c["state"] == "no_setup" and not c["qualified"]
    assert c["signal_pct"] == 0.1


def test_live_pending_then_open():
    forming = [_bar(9, 0, 100000, 100120, 99990, 100100)]
    c = _by_rule(simulate_day(forming, 100000.0, live=True))["H2"]
    assert c["state"] == "pending"
    # Backtest mode must never call a half-formed window a trade.
    c = _by_rule(simulate_day(forming, 100000.0, live=False))["H2"]
    assert c["state"] == "no_data"

    running = forming + [
        _bar(9, 27, 100200, 100210, 100150, 100200),
        _bar(9, 30, 100200, 100260, 100160, 100240),   # touches nothing
    ]
    c = _by_rule(simulate_day(running, 100000.0, live=True))["H2"]
    assert c["state"] == "open" and c["last_px"] == 100240
    assert c["points"] == 40.0 and c["net_rs"] == 40 * 10 - 250


def test_no_prev_close_disables_gap_rule_only():
    bars = [
        _bar(9, 0, 100000, 100120, 99990, 100100),
        _bar(9, 27, 100200, 100210, 100150, 100200),
        _bar(9, 30, 100200, 100600, 100150, 100550),
    ]
    cards = _by_rule(simulate_day(bars, prev_close=None))
    assert cards["H1"]["state"] == "no_data"
    assert cards["H2"]["state"] == "closed"


def test_phase_partition_freeze_day_is_backtest():
    assert rules.phase_of("2026-08-24") == "backtest"
    assert rules.phase_of("2026-08-25") == "backtest"   # the ambiguous day counts as homework
    assert rules.phase_of("2026-08-26") == "forward"


def test_run_ledger_chains_prev_close_across_sessions():
    day1 = [_bar(9, 0, 100000, 100120, 99990, 100000, day=19),
            _bar(23, 27, 100000, 100020, 99980, 100000, day=19)]
    day2 = [
        _bar(9, 0, 100600, 100650, 100500, 100560),
        _bar(9, 12, 100560, 100600, 100500, 100550),
        _bar(9, 15, 100550, 100560, 99990, 100050),
    ]
    frame = pd.DataFrame(day1 + day2)
    cards = rules.run_ledger(frame)
    h1 = [c for c in cards if c["rule"] == "H1"]
    # Day 1 has no previous close; day 2's gap is measured off day 1's last bar.
    assert h1[0]["state"] == "no_data"
    assert h1[1]["state"] == "closed" and h1[1]["target_px"] == 100000.0
    assert all(c["phase"] == "backtest" for c in cards)


def test_summarise_and_exit_mix():
    trades = [
        {"state": "closed", "date": "2026-08-20", "net_rs": 2750.0,
         "gross_rs": 3000.0, "exit_reason": "target"},
        {"state": "closed", "date": "2026-08-21", "net_rs": -1250.0,
         "gross_rs": -1000.0, "exit_reason": "stop"},
    ]
    s = rules.summarise(trades)
    assert s["n"] == 2 and s["win_rate_pct"] == 50.0
    assert s["net_rs"] == 1500 and s["exit_mix"] == {"target": 1, "stop": 1, "time": 0}
    assert s["profit_factor"] == round(2750 / 1250, 2)
    assert rules.summarise([]) == {"n": 0}


def test_live_transitions_fire_only_on_change():
    open_card = {"rule": "H2", "state": "open", "exit_reason": None}
    closed_card = {"rule": "H2", "state": "closed", "exit_reason": "target"}
    quiet = {"rule": "H1", "state": "no_setup", "exit_reason": None}

    assert transitions({}, [open_card]) == [open_card]          # fresh open fires
    prev = states_of([open_card, quiet])
    assert transitions(prev, [open_card, quiet]) == []          # no change, no spam
    assert transitions(prev, [closed_card, quiet]) == [closed_card]
    assert transitions({}, [quiet]) == []                       # no_setup never alerts
