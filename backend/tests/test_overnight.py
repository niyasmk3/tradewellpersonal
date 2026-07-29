"""Overnight-hold ledger tests: the next-open latch and the grading block.

The pattern under audition: hold a positional thesis into the close (the
deliberate late-day entries especially) and let the overnight gap — the one
move no intraday exit can touch — do the work. Evidence lives in three places
pinned here: the card score stored ON the row (entry_score), the first print
of the next session latched by the monitor (next_open_premium), and
summarize()'s `overnight` block that grades positional rows which survived an
IST day boundary, evening subset split out.

Run:  python backend/tests/test_overnight.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.market.calendar import EVENING_MIN, IST
from app.paper.service import summarize
from app.signals.models import (
    Action,
    Direction,
    ScoreBreakdown,
    SignalCard,
    SignalState,
    TradingMode,
)
from app.trades import monitor
from app.trades.store import TradeStore


def _card(cid="O", entry=100.0, mode=TradingMode.POSITIONAL, confidence=81.0):
    at = int(time.time())
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=mode, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24250 CE", strike=24250.0, expiry="2026-08-27",
        entry_low=entry - 1, entry_high=entry + 1, premium_sl=round(entry * 0.70, 2),
        target1=round(entry * 1.60, 2), target2=round(entry * 2.05, 2),
        trailing_sl_rule="r", risk_reward=2.0, confidence=confidence,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 6 * 3600,
        score=ScoreBreakdown(direction=Direction.CE, components=[], total=confidence),
    )


def _store():
    d = tempfile.mkdtemp()
    return TradeStore(path=Path(d) / "p.json")


def _ist_epoch(days_ago: int, hh: int, mm: int) -> int:
    d = datetime.now(IST) - timedelta(days=days_ago)
    return int(d.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())


def _ist_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")


def test_entry_score_on_row():
    """The card's total score is stored ON the trade at fill — the overnight
    ledger grades "score > X", which is unanswerable without it."""
    s = _store()
    t = s.create_from_signal(_card(confidence=79.0), 1, 100.0, 65)
    assert t.entry_score == 79.0
    print("  SCORE  -> card confidence persisted as entry_score at fill")


def test_next_open_latch():
    """First premium print after an IST day boundary latches ONCE; same-day
    cycles, missing ist_date, and stale premiums never latch."""
    s = _store()
    t = s.create_from_signal(_card("L"), 1, 100.0, 65)
    t.entered_at = _ist_epoch(1, 14, 40)          # yesterday, 14:40 IST

    # Same session as entry: nothing to latch.
    monitor.evaluate(t, 101.0, None, 14 * 60 + 41, _ist_day(t.entered_at))
    assert t.next_open_premium is None

    # Stale premium next morning: the latch must wait for a real print.
    monitor.evaluate(t, None, 24260.0, 9 * 60 + 16, _ist_day(_ist_epoch(0, 9, 16)))
    assert t.next_open_premium is None

    # First live print of the new session: latched, with the event on record.
    monitor.evaluate(t, 92.0, None, 9 * 60 + 16, _ist_day(_ist_epoch(0, 9, 16)))
    assert t.next_open_premium == 92.0
    assert t.next_open_at is not None
    assert any(e.kind == "next_open" for e in t.events)

    # Later (higher) prints must not overwrite the open.
    monitor.evaluate(t, 120.0, None, 10 * 60, _ist_day(_ist_epoch(0, 10, 0)))
    assert t.next_open_premium == 92.0

    # No ist_date (legacy callers, tests): latch stays off rather than guessing.
    u = s.create_from_signal(_card("U"), 1, 100.0, 65)
    u.entered_at = _ist_epoch(1, 14, 40)
    monitor.evaluate(u, 95.0, None, 9 * 60 + 16, None)
    assert u.next_open_premium is None
    print("  LATCH  -> next-session first print latched once; stale/None-date never latch")


def test_summarize_overnight_block():
    """Positional rows that crossed an IST day boundary, evening subset split
    at EVENING_MIN; same-day, other-mode and hollow rows stay out."""
    s = _store()

    def _row(cid, mode, entered, exit_px, reason, confidence=81.0,
             next_open=None, notes=None):
        card = _card(cid, mode=mode, confidence=confidence)
        t = s.create_from_signal(card, 1, 100.0, 65, notes=notes)

        def backdate(x):
            if x.id == t.id:
                x.entered_at = entered
                if next_open is not None:
                    x.next_open_premium = next_open
        s.apply_monitor(backdate)
        s.auto_close(t.id, exit_px, reason)
        return t.id

    # Evening winner: in the block, evening subset, gap on record.
    _row("EW", TradingMode.POSITIONAL, _ist_epoch(1, 14, 40), 118.0, "target1",
         confidence=79.0, next_open=112.0)
    # Morning-entry overnight loser: in the block, NOT evening, pre-latch row.
    _row("ML", TradingMode.POSITIONAL, _ist_epoch(1, 11, 0), 90.0, "stop")
    # Same-day positional: never crossed a boundary — out.
    _row("SD", TradingMode.POSITIONAL, _ist_epoch(0, 10, 0), 105.0, "target1")
    # Intraday row forced across the boundary: wrong mode — out.
    _row("ID", TradingMode.INTRADAY, _ist_epoch(1, 14, 40), 118.0, "target1")
    # Hollow positional across the boundary: counterfactual — out.
    _row("HO", TradingMode.POSITIONAL, _ist_epoch(1, 14, 45), 118.0, "target1",
         notes="hollow: oi_floor")

    on = summarize(s)["overnight"]
    assert on is not None
    assert on["trades"] == 2
    assert on["win_rate"] == 50.0
    assert on["evening"] is not None and on["evening"]["trades"] == 1

    by_gap = {r["overnight_move_pct"]: r for r in on["rows"]}
    win = by_gap[12.0]                      # (112 - 100) / 100
    assert win["evening"] is True and win["score"] == 79.0
    assert win["next_open"] == 112.0 and win["net_pnl"] > 0
    lose = by_gap[None]                     # pre-latch row: gap unknowable
    assert lose["evening"] is False and lose["net_pnl"] < 0
    assert on["evening"]["net_pnl"] == win["net_pnl"]
    print("  BLOCK  -> 2 overnight rows graded; evening split; same-day/intraday/hollow out")


def test_overnight_block_none_until_evidence():
    """No positional row across a boundary -> the block is None, not zeros."""
    s = _store()
    t = s.create_from_signal(_card("SD2"), 1, 100.0, 65)
    s.auto_close(t.id, 105.0, "target1")     # same-day close
    assert summarize(s)["overnight"] is None
    print("  EMPTY  -> block is None until a hold survives a day boundary")


def test_evening_min_shared_with_gap_caution():
    """The gap caution and the ledger's evening split key off the SAME minute —
    a warning about a pattern must agree with the ledger grading it."""
    from app.signals.service import overnight_gap_note

    assert overnight_gap_note("positional", EVENING_MIN) is not None
    assert overnight_gap_note("positional", EVENING_MIN - 1) is None
    assert overnight_gap_note("intraday", EVENING_MIN) is None
    print("  SHARED -> EVENING_MIN drives both the caution note and the ledger split")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                fails += 1
                print(f"  FAIL   -> {name}: {exc}")
    if fails:
        print(f"{fails} test(s) FAILED")
        sys.exit(1)
    print("all overnight-ledger tests passed")
