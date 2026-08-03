"""Early-derisk aftermath ledger tests.

The +5% lock went live without a paired A/B; this ledger is the honest
substitute — score each breakeven lock-out by what the premium did in the
same-session post-close window. These tests pin the classification, the
rupee sums, the exclusions, and that paper rows actually GET post-close
tracking (they didn't, before this change).

Run:  python backend/tests/test_derisk_aftermath.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
import time
from pathlib import Path

from app.paper.service import summarize
from app.signals.models import (
    Action, Direction, ScoreBreakdown, SignalCard, SignalState, TradingMode,
)
from app.trades import monitor
from app.trades.models import TradeEvent
from app.trades.store import TradeStore


def _card(cid="D", entry=100.0):
    at = int(time.time())
    return SignalCard(
        id=f"{cid}-{at}", symbol="NIFTY", mode=TradingMode.INTRADAY, title="t",
        action=Action.BUY_CE, direction=Direction.CE, state=SignalState.ACTIVE,
        contract="NIFTY 24000 CE", strike=24000.0, expiry="2026-08-06",
        entry_low=entry - 1, entry_high=entry + 1, premium_sl=round(entry * 0.82, 2),
        target1=round(entry * 1.27, 2), target2=round(entry * 1.45, 2),
        trailing_sl_rule="r", risk_reward=1.5, confidence=81.0,
        underlying_invalidation="u", invalidation_note="n",
        created_at=at, valid_until=at + 480,
        score=ScoreBreakdown(direction=Direction.CE, components=[], total=81.0),
    )


def _store():
    return TradeStore(path=Path(tempfile.mkdtemp()) / "p.json")


def _locked_out(store, cid, post_mfe=None, post_mae=None):
    """A trade the lock ended at ~breakeven, with a given aftermath."""
    t = store.create_from_signal(_card(cid), 1, 100.0, 65, quick_pct=0.12)

    def fn(x):
        x.events.append(TradeEvent(ts=int(time.time()), kind="early_derisk",
                                   note="MFE 105 >= +5% — SL to entry"))
    store._apply(t.id, fn)
    store.auto_close(t.id, 100.2, "stop")            # breakeven-ish stop at entry

    def post(x):
        x.post_close_mfe, x.post_close_mae = post_mfe, post_mae
    store._apply(t.id, post)
    return t


def test_classification_and_sums():
    s = _store()
    # Runner escaped: after the lock-out it ran to 118 (past the 112 QT).
    _locked_out(s, "RUN", post_mfe=118.0, post_mae=99.0)
    # Crash avoided: after the lock-out it fell to 85 (below entry*0.92).
    _locked_out(s, "CRASH", post_mfe=101.0, post_mae=85.0)
    # Noise: drifted 98-104, neither threshold.
    _locked_out(s, "NOISE", post_mfe=104.0, post_mae=98.0)
    # Unobserved: closed at the bell, no post-close data.
    _locked_out(s, "BELL")
    # A derisked trade that WON (not locked out) counts as armed only.
    w = s.create_from_signal(_card("WIN"), 1, 100.0, 65, quick_pct=0.12)
    def fn(x):
        x.events.append(TradeEvent(ts=int(time.time()), kind="early_derisk", note="lock"))
    s._apply(w.id, fn)
    s.auto_close(w.id, 127.0, "target1")

    da = summarize(s)["derisk_aftermath"]
    assert da["armed"] == 5 and da["locked_out"] == 4, da
    assert da["runner_escaped"] == 1 and da["crash_avoided"] == 1
    assert da["noise"] == 1 and da["unobserved"] == 1
    # Sums: forfeited = (118 - 100.2) * 65; avoided = (100.2 - 85) * 65.
    assert abs(da["forfeited"] - (118.0 - 100.2) * 65) < 0.01, da["forfeited"]
    assert abs(da["avoided"] - (100.2 - 85.0) * 65) < 0.01, da["avoided"]
    print("  SCORE  -> escaped/avoided/noise/unobserved classified; sums exact")


def test_exclusions():
    s = _store()
    # Hollow (shadow) rows never enter, even with a lock event.
    h = _locked_out(s, "H", post_mfe=120.0)
    def tag(x): x.notes = "hollow: vol floor"
    s._apply(h.id, tag)
    # A stop-out far from entry is a REAL stop, not a lock-out.
    t = s.create_from_signal(_card("FAR"), 1, 100.0, 65, quick_pct=0.12)
    def fn(x):
        x.events.append(TradeEvent(ts=int(time.time()), kind="early_derisk", note="lock"))
    s._apply(t.id, fn)
    s.auto_close(t.id, 90.0, "stop")                 # -10%: not breakeven

    da = summarize(s)["derisk_aftermath"]
    assert da["armed"] == 1 and da["locked_out"] == 0, da
    s2 = _store()
    assert summarize(s2)["derisk_aftermath"] is None, "no derisked rows -> None"
    print("  EXCL   -> shadows out; far stops are not lock-outs; empty -> None")


def test_paper_rows_get_post_close_tracking():
    """The enabling fix: paper's run_once must observe same-day closed rows
    (track_reversible), or every aftermath field stays None forever."""
    from app.paper.service import PaperTradingService
    from app.config import Settings
    from app.state import MarketState

    store = _store()
    state = MarketState()
    svc = PaperTradingService(Settings(_env_file=None, SIGNAL_MAX_PREMIUM_AGE_S=0),
                              state, store)
    t = store.create_from_signal(_card("PC"), 1, 100.0, 65, quick_pct=0.12)
    store.auto_close(t.id, 100.2, "stop")
    state.ticks[t.token or 0] = {"last_price": 118.5}
    row = store.get(t.id)
    row_token = row.token
    if row_token is None:                     # card fixture carries no token
        def settok(x): x.token = 4242
        store._apply(t.id, settok)
        state.ticks[4242] = {"last_price": 118.5}
    svc.run_once()
    after = store.get(t.id)
    assert after.post_close_mfe == 118.5, after.post_close_mfe
    print("  TRACK  -> paper observes closed rows; post_close_mfe recorded")


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
