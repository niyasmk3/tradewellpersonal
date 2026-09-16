"""submit() + the append-only ledger (16-Sep).

The properties here are about what survives a crash, not about strategy:

  * WRITE-AHEAD — the row that burns the idempotency key and spends the day's
    order budget is written BEFORE the broker call, so a process that dies
    mid-send restarts into "already sent", not into a second entry;
  * a BLOCKED intent costs nothing — no key burned, no budget spent;
  * the ledger rebuilds the day's counters, so a restart does not hand the
    runner a fresh order budget;
  * three rejections in a row trip the kill switch, and a success resets it.

Run:  python backend/tests/test_algo_runner.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
from pathlib import Path

from app.algo import contract
from app.algo.broker import Broker, DryRunBroker, LiveBroker, PaperBroker, for_mode
from app.algo.guard import GuardInput
from app.algo.intent import BrokerResult, OrderIntent, intent_key
from app.algo.killswitch import KillSwitch
from app.algo.ledger import AlgoLedger
from app.algo.runner import RunnerState, guard_input_from, submit

NOW = 1_000_000.0
DAY = "2026-09-16"
MIDDAY = 12 * 60


class RejectingBroker(Broker):
    mode = "dry"

    def place(self, intent):
        return BrokerResult(ok=False, status="REJECTED", message="RMS said no")


class ExplodingBroker(Broker):
    mode = "dry"

    def place(self, intent):
        raise RuntimeError("socket died mid-send")


def _intent(purpose="open", leg="entry_ce", **over):
    kw = dict(key=intent_key("closing", DAY, leg, purpose), strategy="closing",
              day=DAY, leg=leg, purpose=purpose, segment="NFO",
              tradingsymbol="NIFTY26SEP25000CE", side="BUY", order_type="LIMIT",
              quantity=75, lots=1, price=120.50)
    kw.update(over)
    return OrderIntent(**kw)


def _gi(intent, ledger, ks, state=None, now=NOW, **over):
    world = dict(trading_day=True, token_state="valid", feed_healthy=True,
                 last_tick_age_s=2.0, clock_skew_s=0.1)
    world.update(over)
    return guard_input_from(
        intent, now, MIDDAY, ledger=ledger, ks=ks, state=state,
        arm=type("A", (), {"strategy": "closing", "mode": "dry",
                           "expires_at": NOW + 86400})(),
        **world)


def _fixture(td):
    return (AlgoLedger(Path(td) / ".algo_orders.jsonl"),
            KillSwitch(Path(td) / ".algo_killswitch.json"),
            RunnerState())


def test_allowed_submit_writes_the_full_four_row_trail():
    with tempfile.TemporaryDirectory() as td:
        led, ks, st = _fixture(td)
        out = submit(_gi(_intent(), led, ks, st), DryRunBroker(), st, led, ks)
        assert out.placed is True
        assert [r["type"] for r in led.rows(DAY)] == \
            ["intent", "verdict", "order", "result"]
        ds = led.day_state(DAY)
        assert ds.orders_today == 1 and ds.open_orders_today == 1
        assert led.open_mode(_intent().key) == "dry"
        assert [r["key"] for r in led.open_position_keys()] == [_intent().key]
    print("  RUN    -> allowed submit: intent/verdict/order/result, counters move")


def test_blocked_submit_costs_nothing():
    with tempfile.TemporaryDirectory() as td:
        led, ks, st = _fixture(td)
        g = _gi(_intent(), led, ks, st, feed_healthy=False)
        out = submit(g, DryRunBroker(), st, led, ks)
        assert out.placed is False and out.verdict.code == "FEED_UNHEALTHY"
        assert [r["type"] for r in led.rows(DAY)] == ["intent", "verdict"]
        ds = led.day_state(DAY)
        assert ds.orders_today == 0 and not ds.seen_keys
        assert st.recent_order_ts == []
    print("  RUN    -> a blocked intent burns no key and spends no budget")


def test_write_ahead_survives_a_broker_that_explodes():
    """The order row exists even though the call raised: the key is spent and
    the budget consumed, so a restart cannot send a second one."""
    with tempfile.TemporaryDirectory() as td:
        led, ks, st = _fixture(td)
        out = submit(_gi(_intent(), led, ks, st), ExplodingBroker(), st, led, ks)
        assert out.placed is False and out.result.status == "ERROR"
        types = [r["type"] for r in led.rows(DAY)]
        assert "order" in types and types[-1] == "result"
        assert led.day_state(DAY).orders_today == 1
    print("  RUN    -> broker exception still leaves the write-ahead order row")


def test_a_restart_cannot_replay_the_same_decision():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / ".algo_orders.jsonl"
        ks = KillSwitch(Path(td) / ".k.json")
        led, st = AlgoLedger(path), RunnerState()
        assert submit(_gi(_intent(), led, ks, st), DryRunBroker(), st, led, ks).placed
        # "Restart": brand-new ledger object and runner state over the same file.
        led2, st2 = AlgoLedger(path), RunnerState()
        out = submit(_gi(_intent(), led2, ks, st2), DryRunBroker(), st2, led2, ks)
        assert not out.placed and out.verdict.code == "DUPLICATE", out.verdict.blocks
        assert led2.day_state(DAY).orders_today == 1
    print("  RUN    -> restart replays the decision, ledger refuses the repeat")


def test_three_rejections_trip_the_kill_switch_and_a_fill_resets_the_streak():
    with tempfile.TemporaryDirectory() as td:
        led, ks, st = _fixture(td)
        # One order per second: firing them at the same instant would (rightly)
        # be refused by the rate limiter before any broker saw them.
        t = NOW
        for n in range(contract.CONSECUTIVE_REJECTS_KILL - 1):
            t += 1
            assert submit(_gi(_intent(leg="l%d" % n), led, ks, st, now=t),
                          RejectingBroker(), st, led, ks).verdict.allowed
        assert not ks.tripped and st.consecutive_rejects == 2
        t += 1
        submit(_gi(_intent(leg="ok"), led, ks, st, now=t), DryRunBroker(),
               st, led, ks)
        assert st.consecutive_rejects == 0, "a fill must reset the streak"
        for n in range(contract.CONSECUTIVE_REJECTS_KILL):
            t += 1
            submit(_gi(_intent(leg="r%d" % n), led, ks, st, now=t),
                   RejectingBroker(), st, led, ks)
        assert ks.tripped and ks.code == "CONSECUTIVE_REJECTS"
        assert any(r["type"] == "kill" for r in led.rows(DAY))
    print("  RUN    -> %d rejections in a row trip the switch; a fill resets it"
          % contract.CONSECUTIVE_REJECTS_KILL)


def test_exit_names_its_open_and_inherits_that_opens_mode():
    """An overnight exit lands on a different DAY from its open. The exit
    carries the open's key in `closes`; the ledger finds the mode by key on
    any day, and the position leaves the belief set once the exit is acked."""
    with tempfile.TemporaryDirectory() as td:
        led, ks, st = _fixture(td)
        opened = _intent()
        submit(_gi(opened, led, ks, st), DryRunBroker(), st, led, ks)
        assert len(led.open_position_keys("closing")) == 1
        # Next calendar day, different `day`, same leg name — closes by KEY.
        exit_ = _intent("close", side="SELL", day="2026-09-17", closes=opened.key)
        g = _gi(exit_, led, ks, st, now=NOW + 86400)
        assert g.exit_mode == "dry" and g.effective_mode == "dry"
        assert submit(g, DryRunBroker(), st, led, ks).placed is True
        assert led.open_position_keys() == []
        # An exit that names no open, or an unknown one, has no mode -> refused.
        for closes in (None, "closing:2026-09-01:ghost:open"):
            g2 = _gi(_intent("close", leg="x", side="SELL", closes=closes),
                     led, ks, st, now=NOW + 86401)
            assert g2.exit_mode is None
            assert submit(g2, DryRunBroker(), st, led, ks).verdict.code == "EXIT_UNOWNED"
    print("  RUN    -> exit closes its open by key across days; orphans refused")


def test_rate_state_prunes_and_ledger_is_day_scoped_and_corruption_tolerant():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / ".l.jsonl"
        led = AlgoLedger(path)
        led.record("order", DAY, {"key": "a", "purpose": "open", "mode": "dry"})
        led.record("order", "2026-09-15", {"key": "b", "purpose": "open"})
        with open(path, "a") as fh:
            fh.write("{ this line is not json\n")
        led.record("order", DAY, {"key": "c", "purpose": "close"})
        assert led.day_state(DAY).orders_today == 2          # corrupt line skipped
        assert led.day_state(DAY).open_orders_today == 1
        assert led.day_state("2026-09-15").orders_today == 1
        assert len(led.tail(1, DAY)) == 1

        st = RunnerState()
        st.note_sent(NOW)
        st.note_sent(NOW + 100)                              # beyond the window
        assert st.recent_order_ts == [NOW + 100]
    print("  LEDG   -> day-scoped, one corrupt line skipped, rate state pruned")


def test_live_world_reports_skew_only_while_the_market_is_open():
    """After 15:30 Kite still ticks, stamped with the last trade — the
    'skew' would read as the tape's age (7006s, seen 16-Sep 18:02). Outside
    the session the world says None, and the preflight says why."""
    from datetime import datetime
    from app.market import calendar as mcal
    from app.market.calendar import IST
    from app.state import market_state
    from app.algo.runner import live_world

    saved = market_state.tick_skew_s
    try:
        market_state.tick_skew_s = lambda: 7006.86
        closed = live_world(datetime(2026, 9, 16, 18, 2, tzinfo=IST))
        assert closed["clock_skew_s"] is None and closed["market_open"] is False
        assert mcal.is_trading_day(datetime(2026, 9, 16).date())
        market_state.tick_skew_s = lambda: 0.7
        opened = live_world(datetime(2026, 9, 16, 11, 0, tzinfo=IST))
        assert opened["clock_skew_s"] == 0.7 and opened["market_open"] is True
    finally:
        market_state.tick_skew_s = saved
    print("  WORLD  -> skew is None after the close, measured in session")


def test_live_and_paper_brokers_refuse_to_run_yet():
    assert isinstance(for_mode("dry"), DryRunBroker)
    for factory in (lambda: for_mode("live"), lambda: PaperBroker().place(_intent())):
        try:
            factory()
            raise AssertionError("a rung above A1 was reachable")
        except NotImplementedError:
            pass
    try:
        for_mode("turbo")
        raise AssertionError("unknown mode accepted")
    except ValueError:
        pass
    assert LiveBroker.mode == "live"
    print("  BROK   -> dry works; paper and live refuse until their build steps")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
