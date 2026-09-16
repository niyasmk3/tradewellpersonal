"""Brokers — the ONLY place in this repository that may talk to an order API.

Everything upstream deals in intents; this is where an intent becomes (or
deliberately does not become) an order. Keeping it behind one narrow interface
buys three things:

  * the rollout ladder is a constructor argument, not a rewrite — A0 dry-run,
    A1 paper, A2 live are three classes with one method;
  * the static-IP split (plan Part 1, option A) becomes an HTTP call inside
    LiveBroker and changes nothing else in the repo;
  * "does Tradewell place orders?" has a one-file answer, forever.

LiveBroker is intentionally a stub that raises. It lands at step 10, after
reconcile.py exists and after a strategy has cleared its own evidence bar —
and it will need the Kite Connect static-IP whitelist to be in place, because
SEBI's retail algo framework has brokers reject order requests from
unregistered IPs since 01-Apr-2026.
"""
from __future__ import annotations

import abc
import logging
from typing import Optional

from app.algo.intent import BrokerResult, OrderIntent

log = logging.getLogger("tradewell.algo")


class Broker(abc.ABC):
    mode = "unset"

    @abc.abstractmethod
    def place(self, intent: OrderIntent) -> BrokerResult:
        ...


class DryRunBroker(Broker):
    """Logs the intent and returns a synthetic ack. Touches no network.

    Rung A0. The point is not the ack — it is that the whole path upstream
    (adapter, guard, ledger, counters, duplicate defence) runs exactly as it
    will in A2, so the intent stream can be diffed against what the user
    actually did by hand.
    """
    mode = "dry"

    def place(self, intent: OrderIntent) -> BrokerResult:
        log.info("DRY %s %s %s qty=%s @ %s [%s]", intent.side, intent.purpose,
                 intent.tradingsymbol, intent.quantity, intent.price, intent.key)
        return BrokerResult(ok=True, broker_order_id="DRY-" + intent.key,
                            status="ACK", message="dry run — no order placed")


class PaperBroker(Broker):
    """Rung A1: routes into the existing paper simulator, which already models
    slippage and the full Zerodha charge schedule. Wired at build step 8."""
    mode = "paper"

    def place(self, intent: OrderIntent) -> BrokerResult:
        raise NotImplementedError(
            "PaperBroker lands at build step 8 — see docs/algo-tab-plan-2026-09-16.md")


class LiveBroker(Broker):
    """Rung A2+. Real orders, real money, from the whitelisted static IP."""
    mode = "live"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "LiveBroker lands at build step 10, and is not authorised by the "
            "current plan. Nothing above rung A1 is approved.")

    def place(self, intent: OrderIntent) -> BrokerResult:  # pragma: no cover
        raise NotImplementedError


def for_mode(mode: str, **kwargs) -> Broker:
    if mode == "dry":
        return DryRunBroker()
    if mode == "paper":
        return PaperBroker()
    if mode == "live":
        return LiveBroker(**kwargs)
    raise ValueError("unknown broker mode: %r" % (mode,))
