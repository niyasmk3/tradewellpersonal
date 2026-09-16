"""submit() — the one path from an intent to an order, and everything that has
to happen around it.

There is no second path. Adapters (build step 4) turn a strategy's card into an
OrderIntent and call this; nothing else may call a broker. The asyncio loop
that drives the adapters on a clock lands with them — a loop with nothing to
run would be dead code today.

WRITE-AHEAD. The ledger row that burns the idempotency key and consumes the
day's order budget is written BEFORE the broker is called, not after. If the
process dies between the write and the reply, the restart sees a spent key and
declines to send a second order — which is the failure we can live with. The
other ordering loses that protection exactly when it matters most.

REJECTIONS DO NOT AUTO-RETRY. A rejected order burns its key like any other.
Three rejections in a row trip the kill switch: consecutive rejections mean
the runner's model of what the broker will accept is wrong, and continuing to
guess against a live account is how one bad assumption becomes forty orders.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from app.algo import contract
from app.algo.broker import Broker
from app.algo.guard import GuardInput, evaluate
from app.algo.intent import BrokerResult, SubmitOutcome
from app.algo.killswitch import KillSwitch
from app.algo.ledger import AlgoLedger

log = logging.getLogger("tradewell.algo")


@dataclass
class RunnerState:
    """The small mutable state the ledger cannot cheaply rebuild per-order.

    Order timestamps drive the per-second rate limit and are pruned to the
    window; the reject streak is intentionally in-memory, so a restart (a
    human event) gives the streak a fresh start while the kill switch — which
    is NOT in memory — keeps whatever it latched.
    """
    recent_order_ts: List[float] = field(default_factory=list)
    consecutive_rejects: int = 0

    def note_sent(self, now: float) -> None:
        self.recent_order_ts.append(now)
        cutoff = now - max(contract.RATE_WINDOW_S, 5.0)
        self.recent_order_ts = [t for t in self.recent_order_ts if t >= cutoff]


def submit(g: GuardInput, broker: Broker, state: RunnerState,
           ledger: AlgoLedger, ks: KillSwitch) -> SubmitOutcome:
    """Record, gate, send, record. The whole execution path, in one function."""
    intent, now, day = g.intent, g.now, g.intent.day

    ledger.record("intent", day, {
        "key": intent.key, "strategy": intent.strategy, "leg": intent.leg,
        "purpose": intent.purpose, "tradingsymbol": intent.tradingsymbol,
        "side": intent.side, "order_type": intent.order_type,
        "quantity": intent.quantity, "lots": intent.lots, "price": intent.price,
        "note": intent.note,
    }, now=now)

    verdict = evaluate(g)
    ledger.record("verdict", day, {
        "key": intent.key, "allowed": verdict.allowed, "code": verdict.code,
        "reason": verdict.reason, "blocks": list(verdict.blocks),
        "mode": g.effective_mode,
    }, now=now)

    if not verdict.allowed:
        log.info("algo BLOCKED %s: %s — %s", intent.key, verdict.code, verdict.reason)
        return SubmitOutcome(intent=intent, verdict=verdict)

    # Write-ahead: the key is spent and the budget consumed from here on,
    # whatever happens to the call below.
    mode = g.effective_mode or broker.mode
    state.note_sent(now)
    ledger.record("order", day, {
        "key": intent.key, "strategy": intent.strategy, "leg": intent.leg,
        "purpose": intent.purpose, "mode": mode, "closes": intent.closes,
        "tradingsymbol": intent.tradingsymbol, "side": intent.side,
        "quantity": intent.quantity, "lots": intent.lots, "price": intent.price,
        "status": "SENT",
    }, now=now)

    try:
        result = broker.place(intent)
    except Exception as exc:
        # An exception is not "unknown, try again" — it is a rejection whose
        # cause we could not even read. Treat it exactly as harshly.
        log.error("algo broker raised on %s: %s", intent.key, exc)
        result = BrokerResult(ok=False, status="ERROR", message=str(exc))

    ledger.record("result", day, {
        "key": intent.key, "ok": result.ok, "status": result.status,
        "broker_order_id": result.broker_order_id, "message": result.message,
    }, now=now)

    killed = False
    if result.ok:
        state.consecutive_rejects = 0
    else:
        state.consecutive_rejects += 1
        if state.consecutive_rejects >= contract.CONSECUTIVE_REJECTS_KILL:
            killed = ks.trip(
                "CONSECUTIVE_REJECTS",
                "%d rejections in a row, last: %s %s" % (
                    state.consecutive_rejects, result.status, result.message),
                now=now)
            if killed:
                ledger.record("kill", day, {
                    "code": "CONSECUTIVE_REJECTS", "key": intent.key,
                    "streak": state.consecutive_rejects,
                }, now=now)

    return SubmitOutcome(intent=intent, verdict=verdict, result=result, killed=killed)


def guard_input_from(intent, now: float, ist_minute: int, *, ledger: AlgoLedger,
                     ks: KillSwitch, arm=None, state: Optional[RunnerState] = None,
                     **world) -> GuardInput:
    """Assemble a GuardInput from the pieces the runner owns.

    The caller still supplies the world it alone can see (token state, feed
    health, P&L, reconcile time) — the gate stays pure, and this helper only
    saves every call site from re-deriving the ledger-owned counters.
    """
    day_state = ledger.day_state(intent.day)
    world.setdefault("open_positions", len(ledger.open_position_keys()))
    return GuardInput(
        intent=intent, now=now, ist_minute=ist_minute,
        arm_strategy=getattr(arm, "strategy", None),
        arm_mode=getattr(arm, "mode", None),
        arm_expires_at=getattr(arm, "expires_at", None),
        exit_mode=ledger.open_mode(intent.closes) if intent.is_exit else None,
        kill_code=ks.code,
        orders_today=day_state.orders_today,
        open_orders_today=day_state.open_orders_today,
        seen_keys=frozenset(day_state.seen_keys),
        recent_order_ts=tuple(state.recent_order_ts) if state else (),
        **world)


# --- the live wiring -------------------------------------------------------------
# Everything below touches the world (clock, feed, Kite, singletons). It is kept
# at the bottom, after the pure pipeline, so a reader can see exactly where the
# testable part ends.

ADAPTERS = ("closing",)                 # strategies an arm may name today
runner_state = RunnerState()


def live_world(now_ist) -> dict:
    """The facts only the live process can see, in GuardInput's vocabulary.

    day_pnl_rs and reconciled_at are honest Nones/zeros until reconcile.py
    exists (build step 9): the guard makes STALE_RECONCILE a live-only check
    and the loss cap only bites when a cap is set, so dry-run is unaffected
    and live stays blocked — which is the correct state for a runner that
    cannot yet read its own P&L.
    """
    from app.kite.client import kite_service
    from app.market import calendar as mcal
    from app.services import feed
    from app.state import market_state

    # Skew is only measurable against a LIVE tape. After the close Kite keeps
    # sending ticks whose exchange stamp is the last trade (15:29), so the
    # reading becomes the tick's age (seen: "skew 7006s" at 18:02) and would
    # mislead the preflight. Outside the session it is honestly unknown.
    open_now = mcal.is_market_open(now_ist)
    return dict(
        trading_day=mcal.is_trading_day(now_ist.date()),
        token_state=kite_service.validate_token(),
        feed_healthy=feed.healthy,
        last_tick_age_s=market_state.last_tick_age(),
        clock_skew_s=market_state.tick_skew_s() if open_now else None,
        market_open=open_now,
        day_pnl_rs=0.0,
        reconciled_at=None,
    )


def _page(title: str, body: str) -> None:
    try:
        from app.config import get_settings
        from app.notify import push_text
        push_text(title, body, get_settings(), priority="high")
    except Exception:  # pragma: no cover - the pager must not take the loop down
        log.debug("algo page failed", exc_info=True)


def submit_live(intent, now_ts: float, ist_minute: int, now_ist=None):
    """The adapter's submit_fn: assemble the live GuardInput and run submit()
    against the broker the ARM names. Returns None when nothing is armed —
    an adapter running without an arm has nothing to hand an order to."""
    from datetime import datetime

    from app.algo import broker as brokers
    from app.algo.arm import arm_store
    from app.algo.killswitch import kill_switch
    from app.algo.ledger import algo_ledger
    from app.market.calendar import IST

    arm = arm_store.current(now=now_ts)
    if arm is None:
        return None
    now_ist = now_ist or datetime.fromtimestamp(now_ts, IST)
    # For an exit the broker must be the one the OPEN went to, not the arm's.
    mode = algo_ledger.open_mode(intent.closes) if intent.is_exit else arm.mode
    if mode is None:
        mode = arm.mode
    world = live_world(now_ist)
    world.pop("market_open", None)          # preflight-only; not a guard input
    g = guard_input_from(intent, now_ts, ist_minute, ledger=algo_ledger,
                         ks=kill_switch, arm=arm, state=runner_state, **world)
    out = submit(g, brokers.for_mode(mode), runner_state, algo_ledger, kill_switch)
    if out.killed:
        _page("ALGO KILL SWITCH", "Tripped: %s. The runner has stopped, exits "
              "included — any open position is yours to manage in Kite." %
              kill_switch.code)
    return out


async def algo_loop(poll_s: Optional[float] = None) -> None:
    """App-lifetime loop. Idles while disarmed; while armed, gives the armed
    strategy's adapter one tick per poll. An UNEXPECTED exception inside a
    tick trips the kill switch: expected failures (no card yet, no quote)
    are handled inside the adapter as logged skips, so anything that reaches
    here means the runner's own code is wrong, and a runner whose code is
    wrong does not get another tick until a human has looked."""
    import asyncio
    from datetime import datetime

    from app.algo.adapters.closing import ClosingAdapter
    from app.algo.arm import arm_store
    from app.algo.killswitch import kill_switch
    from app.algo.ledger import algo_ledger
    from app.config import get_settings
    from app.market.calendar import IST

    adapters = {"closing": ClosingAdapter(submit_live)}
    while True:
        try:
            interval = poll_s or get_settings().algo_poll_s
            arm = arm_store.current()
            if arm is None or kill_switch.tripped:
                await asyncio.sleep(max(interval, 15.0))
                continue
            adapter = adapters.get(arm.strategy)
            if adapter is None:
                await asyncio.sleep(interval)
                continue
            now = datetime.now(IST)
            positions = algo_ledger.open_position_keys(arm.strategy)
            try:
                await asyncio.to_thread(adapter.tick, now, algo_ledger, positions)
            except Exception as exc:
                if kill_switch.trip("RUNNER_EXCEPTION", "%s: %s" % (
                        type(exc).__name__, exc)):
                    algo_ledger.record("kill", now.date().isoformat(),
                                       {"code": "RUNNER_EXCEPTION", "detail": str(exc)})
                    _page("ALGO KILL SWITCH", "Runner raised %s: %s. Stopped until "
                          "cleared." % (type(exc).__name__, exc))
                log.exception("algo runner tick raised — kill switch tripped")
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the loop must outlive its own bugs
            log.debug("algo loop iteration failed", exc_info=True)
            await asyncio.sleep(30)
