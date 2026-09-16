"""The plain-data vocabulary the execution path speaks.

Deliberately dataclasses, not pydantic models: these cross no HTTP boundary
(the route layer converts at the edge), and the guard's whole claim to being
testable is that it is pure functions over plain data — no validators, no
coercion, no clock hiding in a default.

PURPOSE is the axis everything else hangs off. An `open` is discretionary: it
can always wait for tomorrow, so nearly every guard blocks it. A `close` is
not: the position already exists, and a gate that refuses to let you out is
more dangerous than no gate at all. The guard applies two different lists,
and `is_exit` is what selects between them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

# "open"    — establish a new position (discretionary; fully gated)
# "close"   — exit a position the runner believes it holds
# "flatten" — forced exit at the hard-flatten clock; gated like a close
PURPOSES = ("open", "close", "flatten")


def intent_key(strategy: str, day: str, leg: str, purpose: str) -> str:
    """The idempotency key: one per (strategy, day, leg, purpose), forever.

    This is the single defence against the failure that costs the most and
    looks the most innocent — a restart, a duplicate tick, or a retry after a
    timeout producing a SECOND entry in the same trade. The key is derived,
    never random, so the same decision computed twice collides on purpose.
    """
    return "%s:%s:%s:%s" % (strategy, day, leg, purpose)


@dataclass(frozen=True)
class OrderIntent:
    """What the runner wants done, before anyone has decided whether it may."""
    key: str                    # idempotency key — see intent_key()
    strategy: str               # "closing" | "gold" | ...
    day: str                    # IST trading date, YYYY-MM-DD
    leg: str                    # free-form within a strategy, e.g. "entry_ce"
    purpose: str                # one of PURPOSES
    segment: str                # "NFO" | "MCX" | ...
    tradingsymbol: str
    side: str                   # "BUY" | "SELL"
    order_type: str             # "LIMIT" | "MARKET" | ...
    quantity: int               # in units, not lots
    lots: int
    price: Optional[float] = None       # required for LIMIT
    index_linked: bool = True           # priced off an index print -> CAS applies
    # An exit names the OPEN it closes by that open's key. Overnight strategies
    # exit on a different calendar day from their entry, so "same day, same
    # leg" cannot find the position — the key can, on any day.
    closes: Optional[str] = None
    note: str = ""

    @property
    def is_exit(self) -> bool:
        return self.purpose in ("close", "flatten")

    @property
    def notional_rs(self) -> float:
        return float(self.quantity) * float(self.price or 0.0)


@dataclass(frozen=True)
class GuardVerdict:
    """Why an intent may or may not proceed.

    `blocks` carries EVERY failing check, not just the first. In dry-run that
    is the point of the whole exercise: "which gates would have fired, how
    often" is the evidence that says whether the gate set is calibrated or
    merely decorative.
    """
    allowed: bool
    code: str = "OK"
    reason: str = ""
    blocks: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BrokerResult:
    ok: bool
    broker_order_id: Optional[str] = None
    status: str = ""            # "ACK" | "REJECTED" | "ERROR"
    message: str = ""


@dataclass(frozen=True)
class SubmitOutcome:
    """The full record of one submit() call — what the UI and the ledger show."""
    intent: OrderIntent
    verdict: GuardVerdict
    result: Optional[BrokerResult] = None
    killed: bool = False        # did THIS submit trip the kill switch?

    @property
    def placed(self) -> bool:
        return self.result is not None and self.result.ok
