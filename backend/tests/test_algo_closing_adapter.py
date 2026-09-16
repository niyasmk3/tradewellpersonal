"""Closing Day adapter (16-Sep): the logged card -> two orders a day.

What is pinned: the adapter reads the LOGGED card and adds no rule; the
policy skip is logged once, not every tick; a missing card is "not yet" and
is retried; entry and exit prices snap to the tick on the marketable side;
an exit names its open by key so an overnight position can be closed on a
later day; and no tick ever calls the broker directly — every intent goes
through the injected submit.

Run:  python backend/tests/test_algo_closing_adapter.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app.algo import contract
from app.algo.adapters import closing as ad
from app.algo.intent import intent_key
from app.algo.ledger import AlgoLedger
from app.market.calendar import IST

DAY = "2026-09-16"                       # Wednesday, a trading day
NEXT = "2026-09-17"
CARD = {"date": DAY, "as_of": DAY + "T15:05:25+05:30", "verdict": "CLEAN", "red": [],
        "values": {"direction": "CE", "strike": 25000.0, "expiry": "2026-09-22",
                   "next_trading_day": NEXT}}
FLAGGED = dict(CARD, verdict="FLAGGED", red=["lasthr", "volexp"])


def _at(day: str, h: int, m: int, s: int = 0) -> datetime:
    y, mo, d = (int(x) for x in day.split("-"))
    return datetime(y, mo, d, h, m, s, tzinfo=IST)


class _Outcome:
    def __init__(self, placed, code):
        self.placed = placed
        self.verdict = type("V", (), {"code": code})()


class Spy:
    """Stands in for runner.submit_live: records what the adapter asked for
    and answers with a placed or blocked outcome, as submit() would."""
    def __init__(self, placed=True, code="OK"):
        self.intents = []
        self.placed, self.code = placed, code

    def __call__(self, intent, now_ts, ist_min):
        self.intents.append(intent)
        return _Outcome(self.placed, self.code)


def _adapter(spy, card=CARD, ltp=120.0, resolved=("NIFTY26SEP25000CE", 75, 111)):
    res = ad.Resolved(*resolved) if resolved else None
    return ad.ClosingAdapter(
        spy, card_for=lambda d: card,
        resolve=lambda c: res,
        quote_fn=lambda tsym, tok: ltp)


# --- pure builders -------------------------------------------------------------

def test_entry_policy_reads_the_logged_verdict_only():
    assert ad.entry_decision(None) == (False, "no settled 15:05 card logged for today yet")
    go, why = ad.entry_decision(FLAGGED)
    assert not go and "CLEAN only" in why and "lasthr" in why
    flat = dict(CARD, values=dict(CARD["values"], direction=None))
    assert ad.entry_decision(flat)[0] is False
    assert ad.entry_decision(CARD)[0] is True
    print("  ADAPT  -> policy: CLEAN fires, FLAGGED/flat/missing do not")


def test_entry_is_one_lot_buy_limit_snapped_up_through_the_tape():
    i = ad.build_entry(CARD, "NIFTY26SEP25000CE", 75, 120.02, DAY)
    assert (i.side, i.order_type, i.purpose, i.segment) == ("BUY", "LIMIT", "open", "NFO")
    assert i.quantity == 75 and i.lots == 1
    # 120.02 * 1.005 = 120.62 -> snapped UP to 120.65
    assert i.price == 120.65 and contract.is_tick_aligned(i.price)
    assert i.key == intent_key("closing", DAY, "overnight", "open")
    assert i.closes is None and i.index_linked
    print("  ADAPT  -> entry: BUY 1 lot LIMIT 120.65 from tape 120.02")


def test_exit_names_its_open_and_snaps_down():
    open_row = {"key": intent_key("closing", DAY, "overnight", "open"), "day": DAY,
                "tradingsymbol": "NIFTY26SEP25000CE", "quantity": 75, "lots": 1}
    i = ad.build_exit(open_row, 131.87, NEXT)
    assert (i.side, i.purpose, i.day) == ("SELL", "close", NEXT)
    assert i.closes == open_row["key"]
    assert i.key == intent_key("closing", DAY, "overnight", "close")   # the OPEN's day
    # 131.87 * 0.995 = 131.21 -> snapped DOWN to 131.20
    assert i.price == 131.20 and i.quantity == 75
    assert "LATE" not in i.note
    late = ad.build_exit(open_row, 131.87, "2026-09-18")
    assert "LATE" in late.note
    print("  ADAPT  -> exit: SELL LIMIT 131.20, closes its open by key, flags late")


# --- tick() --------------------------------------------------------------------

def test_no_card_means_not_yet_and_is_retried_without_a_skip_row():
    with tempfile.TemporaryDirectory() as td:
        led = AlgoLedger(Path(td) / ".l.jsonl")
        spy = Spy()
        a = _adapter(spy, card=None)
        a.tick(_at(DAY, 15, 6), led, [])
        a.tick(_at(DAY, 15, 8), led, [])
        assert spy.intents == [] and led.rows() == []
        # ...and the moment the card lands, it fires.
        a._card_for = lambda d: CARD
        a.tick(_at(DAY, 15, 9), led, [])
        assert len(spy.intents) == 1 and spy.intents[0].purpose == "open"
    print("  ADAPT  -> no card: silent retry each tick; fires once it is logged")


def test_policy_skip_is_logged_once_per_day_not_per_tick():
    with tempfile.TemporaryDirectory() as td:
        led = AlgoLedger(Path(td) / ".l.jsonl")
        spy = Spy()
        a = _adapter(spy, card=FLAGGED)
        for m in (5, 6, 7, 8, 9, 10, 11):
            a.tick(_at(DAY, 15, m), led, [])
        skips = [r for r in led.rows(DAY) if r["type"] == "skip"]
        assert spy.intents == [] and len(skips) == 1, skips
        assert "CLEAN only" in skips[0]["reason"]
    print("  ADAPT  -> FLAGGED night: zero intents, exactly one skip row")


def test_clean_card_submits_one_entry_and_stops_once_placed():
    with tempfile.TemporaryDirectory() as td:
        led = AlgoLedger(Path(td) / ".l.jsonl")
        spy = Spy(placed=True)
        a = _adapter(spy)
        a.tick(_at(DAY, 15, 4), led, [])                    # before the window
        assert spy.intents == []
        a.tick(_at(DAY, 15, 5), led, [])
        assert len(spy.intents) == 1
        for m in (6, 7, 8, 9, 10, 11, 12):                  # placed: never again
            a.tick(_at(DAY, m // 60 * 0 + 15, m, 30), led, [])
        assert len(spy.intents) == 1
        a.tick(_at(DAY, 15, 13), led, [])                    # window closed
        assert len(spy.intents) == 1
        assert spy.intents[0].tradingsymbol == "NIFTY26SEP25000CE"
        assert spy.intents[0].note.startswith("CLEAN CE 25000.0")
    print("  ADAPT  -> CLEAN: one entry at 15:05; a placed key is done for the day")


def test_blocked_entry_is_retried_on_spacing_but_a_spent_key_is_not():
    with tempfile.TemporaryDirectory() as td:
        led = AlgoLedger(Path(td) / ".l.jsonl")
        spy = Spy(placed=False, code="TOKEN_INVALID")        # a block that may clear
        a = _adapter(spy)
        a.tick(_at(DAY, 15, 5), led, [])
        a.tick(_at(DAY, 15, 5, 30), led, [])                 # inside retry spacing
        assert len(spy.intents) == 1
        a.tick(_at(DAY, 15, 6, 30), led, [])                 # spacing elapsed: retry
        assert len(spy.intents) == 2
        # The ledger already holds this key (e.g. placed before a restart):
        # DUPLICATE is final, so the adapter stops asking.
        spy.placed, spy.code = False, "DUPLICATE"
        a.tick(_at(DAY, 15, 7, 30), led, [])
        assert len(spy.intents) == 3
        a.tick(_at(DAY, 15, 8, 30), led, [])
        assert len(spy.intents) == 3
    print("  ADAPT  -> blocked: retried %.0fs apart; DUPLICATE ends the retries"
          % ad.RETRY_SPACING_S)


def test_unresolvable_contract_or_missing_quote_logs_a_skip():
    with tempfile.TemporaryDirectory() as td:
        led = AlgoLedger(Path(td) / ".l.jsonl")
        spy = Spy()
        _adapter(spy, resolved=None).tick(_at(DAY, 15, 6), led, [])
        _adapter(spy, ltp=None).tick(_at(DAY, 15, 6), led, [])
        reasons = [r["reason"] for r in led.rows(DAY) if r["type"] == "skip"]
        assert spy.intents == [] and len(reasons) == 2
        assert reasons[0].startswith("could not resolve") and reasons[1].startswith("no quote")
    print("  ADAPT  -> no contract / no quote: skip rows, nothing submitted")


def test_exit_window_closes_every_held_position_and_nothing_else():
    open_row = {"key": intent_key("closing", DAY, "overnight", "open"), "day": DAY,
                "tradingsymbol": "NIFTY26SEP25000CE", "quantity": 75, "lots": 1,
                "token": 111}
    with tempfile.TemporaryDirectory() as td:
        led = AlgoLedger(Path(td) / ".l.jsonl")
        spy = Spy()
        a = _adapter(spy, ltp=131.87)
        a.tick(_at(NEXT, 9, 49), led, [open_row])            # before the window
        assert spy.intents == []
        a.tick(_at(NEXT, 9, 50), led, [open_row])
        a.tick(_at(NEXT, 9, 52), led, [open_row])            # still listed: placed, so no repeat
        assert len(spy.intents) == 1
        i = spy.intents[0]
        assert i.purpose == "close" and i.closes == open_row["key"] and i.day == NEXT
        a.tick(_at(NEXT, 10, 6), led, [open_row])            # window closed
        assert len(spy.intents) == 1
        # No positions: the exit window is a no-op.
        a.tick(_at(NEXT, 9, 55), led, [])
        assert len(spy.intents) == 1
    print("  ADAPT  -> exit window: one SELL per held position, none otherwise")


def test_windows_never_overlap_the_contracts_own_clocks():
    """The entry window must sit inside contract.ENTRY_WINDOW_MIN and end
    before the hard-flatten / CAS clocks, or the guard would refuse every
    entry this adapter ever builds."""
    lo, hi = ad.ENTRY_WINDOW_MIN
    assert contract.ENTRY_WINDOW_MIN[0] <= lo and hi <= contract.ENTRY_WINDOW_MIN[1]
    assert hi < contract.HARD_FLATTEN_MIN and hi < contract.CAS_FREEZE_MIN[0]
    assert ad.LOTS <= contract.MAX_LOTS_PER_ORDER
    assert "LIMIT" in contract.ALLOWED_ORDER_TYPES and "NFO" in contract.ALLOWED_SEGMENTS
    print("  ADAPT  -> adapter clocks and sizes fit inside the contract")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
