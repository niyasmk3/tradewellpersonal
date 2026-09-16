"""The pre-trade gate — pure functions over plain data, and the largest test
file in the module by design.

NO CLOCK, NO NETWORK, NO GLOBALS. Everything the gate needs arrives in a
GuardInput that the caller assembles; the gate itself cannot reach the market,
the broker or `time.time()`. That is what lets every one of these rules be
pinned by a test that runs in milliseconds, which is the only way a rule set
this size stays correct as it grows.

TWO LISTS, NOT ONE. An `open` is discretionary — it can always wait for
tomorrow — so nearly every check applies to it. An exit is not discretionary:
the position already exists, and a gate that refuses to let you out is worse
than no gate. So caps, clocks and staleness checks are OPEN-only, and exits
face just the checks that are about whether the order is even placeable
(kill switch, token, rate limit, duplicate, malformed order).

ORDER BUDGET RESERVES EXITS. The hard daily ceiling counts every order; the
open-only cap sits EXIT_RESERVE_ORDERS below it. A day cannot spend its whole
budget on entries and then find it cannot pay to get out.

EVERY failing check is reported, not just the first. In dry-run that is the
entire point: "which gates fire, and how often" is the evidence that says
whether this set is calibrated or merely decorative.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Optional, Tuple

from app.algo import contract
from app.algo.intent import PURPOSES, GuardVerdict, OrderIntent


@dataclass(frozen=True)
class GuardInput:
    """Everything the gate is allowed to know. Assembled by the caller."""
    intent: OrderIntent
    now: float
    ist_minute: int                         # minutes since IST midnight
    trading_day: bool = True

    # arm state (opens)
    arm_strategy: Optional[str] = None
    arm_mode: Optional[str] = None
    arm_expires_at: Optional[float] = None

    # the mode the position being exited was OPENED in. None means the runner
    # does not believe it holds this position, which is itself a block.
    exit_mode: Optional[str] = None

    kill_code: Optional[str] = None
    token_state: str = "unknown"            # 'valid' | 'invalid' | 'unknown'
    feed_healthy: bool = False
    last_tick_age_s: Optional[float] = None
    clock_skew_s: Optional[float] = None

    orders_today: int = 0                   # every order, entries and exits
    open_orders_today: int = 0              # purpose == "open" only
    open_positions: int = 0
    day_pnl_rs: float = 0.0                 # realised + unrealised; negative = loss

    seen_keys: FrozenSet[str] = field(default_factory=frozenset)
    recent_order_ts: Tuple[float, ...] = field(default_factory=tuple)
    reconciled_at: Optional[float] = None

    @property
    def effective_mode(self) -> Optional[str]:
        """Which broker this order would go to: the arm's for an open, the
        position's own for an exit."""
        return self.exit_mode if self.intent.is_exit else self.arm_mode


# --- the checks ---------------------------------------------------------------
# Each returns a reason string when it BLOCKS, or None when it passes. Reasons
# carry the numbers: a block reason without the number is useless at 3pm.

def _killed(g: GuardInput):
    if g.kill_code:
        return ("kill switch tripped (%s) — the runner emits nothing, including "
                "exits, until a human clears it" % g.kill_code)
    return None


def _bad_purpose(g: GuardInput):
    if g.intent.purpose not in PURPOSES:
        return "unknown purpose %r" % g.intent.purpose
    return None


def _segment(g: GuardInput):
    if g.intent.segment not in contract.ALLOWED_SEGMENTS:
        return "segment %s is not in the allowlist %s" % (
            g.intent.segment, sorted(contract.ALLOWED_SEGMENTS))
    return None


def _order_type(g: GuardInput):
    if g.intent.order_type not in contract.ALLOWED_ORDER_TYPES:
        return "order type %s is not in the allowlist %s" % (
            g.intent.order_type, sorted(contract.ALLOWED_ORDER_TYPES))
    return None


def _quantity(g: GuardInput):
    if g.intent.quantity <= 0 or g.intent.lots <= 0:
        return "quantity/lots must be positive (got qty=%s lots=%s)" % (
            g.intent.quantity, g.intent.lots)
    return None


def _price(g: GuardInput):
    if g.intent.order_type != "LIMIT":
        return None
    p = g.intent.price
    if p is None or p <= 0:
        return "LIMIT order needs a positive price (got %r)" % (p,)
    if not contract.is_tick_aligned(p):
        return ("price %.4f is not a multiple of the Rs%.2f tick — the exchange "
                "rejects it" % (p, contract.TICK_SIZE))
    return None


def _duplicate(g: GuardInput):
    if g.intent.key in g.seen_keys:
        return "idempotency key %s already used today — this is a repeat of a " \
               "decision already acted on" % g.intent.key
    return None


def _rate(g: GuardInput):
    recent = [t for t in g.recent_order_ts if g.now - t < contract.RATE_WINDOW_S]
    if len(recent) >= contract.MAX_ORDERS_PER_SEC:
        return "%d orders already in the last %.0fs (cap %d/s)" % (
            len(recent), contract.RATE_WINDOW_S, contract.MAX_ORDERS_PER_SEC)
    return None


def _day_order_cap(g: GuardInput):
    if g.orders_today >= contract.MAX_ORDERS_PER_DAY:
        return "day order ceiling reached (%d/%d, exits included)" % (
            g.orders_today, contract.MAX_ORDERS_PER_DAY)
    return None


def _token(g: GuardInput):
    if g.token_state != "valid":
        return "Kite token state is %r — only a PROBED 'valid' may place orders" % (
            g.token_state,)
    return None


def _exit_unowned(g: GuardInput):
    if g.exit_mode is None:
        return "no recorded open for this position — the runner does not believe " \
               "it holds what this order would close"
    if g.exit_mode not in contract.MODES:
        return "recorded open mode %r is not a known mode" % (g.exit_mode,)
    return None


def _armed(g: GuardInput):
    if g.arm_strategy is None:
        return "not armed"
    if g.arm_strategy != g.intent.strategy:
        return "armed for %r, this intent is %r — an arm authorises exactly one " \
               "strategy" % (g.arm_strategy, g.intent.strategy)
    return None


def _arm_expired(g: GuardInput):
    if g.arm_strategy is None:
        return None                     # _armed already reports it
    if g.arm_expires_at is None or g.arm_expires_at <= g.now:
        return "arm expired %.0fs ago" % (g.now - (g.arm_expires_at or g.now))
    return None


def _mode(g: GuardInput):
    if g.arm_mode not in contract.MODES:
        return "unknown arm mode %r" % (g.arm_mode,)
    return None


def _trading_day(g: GuardInput):
    return None if g.trading_day else "not a trading day"


def _window(g: GuardInput):
    lo, hi = contract.ENTRY_WINDOW_MIN
    if not (lo <= g.ist_minute <= hi):
        return "%02d:%02d is outside the entry window %02d:%02d-%02d:%02d IST" % (
            g.ist_minute // 60, g.ist_minute % 60, lo // 60, lo % 60, hi // 60, hi % 60)
    return None


def _cas(g: GuardInput):
    lo, hi = contract.CAS_FREEZE_MIN
    if g.intent.index_linked and lo <= g.ist_minute <= hi:
        return "inside the %02d:%02d-%02d:%02d closing-auction freeze — the index " \
               "print this order is derived from is not moving" % (
                   lo // 60, lo % 60, hi // 60, hi % 60)
    return None


def _past_flatten(g: GuardInput):
    if g.ist_minute >= contract.HARD_FLATTEN_MIN:
        return "past the %02d:%02d hard-flatten clock — no new positions" % (
            contract.HARD_FLATTEN_MIN // 60, contract.HARD_FLATTEN_MIN % 60)
    return None


def _feed(g: GuardInput):
    return None if g.feed_healthy else "feed is not healthy — the engine is blind"


def _tick_stale(g: GuardInput):
    age = g.last_tick_age_s
    if age is None:
        return "no tick seen this process"
    if age > contract.MAX_TICK_AGE_S:
        return "last tick is %.0fs old (max %.0fs)" % (age, contract.MAX_TICK_AGE_S)
    return None


def _clock(g: GuardInput):
    skew = g.clock_skew_s
    if skew is None:
        return "clock skew unknown"
    if abs(skew) > contract.MAX_CLOCK_SKEW_S:
        return "clock skew %.1fs exceeds %.1fs — every time-based rule is suspect" % (
            skew, contract.MAX_CLOCK_SKEW_S)
    return None


def _open_order_cap(g: GuardInput):
    if g.open_orders_today >= contract.MAX_OPEN_ORDERS_PER_DAY:
        return "entry budget spent (%d/%d) — the remaining %d orders are reserved " \
               "for exits" % (g.open_orders_today, contract.MAX_OPEN_ORDERS_PER_DAY,
                              contract.EXIT_RESERVE_ORDERS)
    return None


def _position_cap(g: GuardInput):
    if g.open_positions >= contract.MAX_OPEN_POSITIONS:
        return "already holding %d/%d positions" % (
            g.open_positions, contract.MAX_OPEN_POSITIONS)
    return None


def _lot_cap(g: GuardInput):
    if g.intent.lots > contract.MAX_LOTS_PER_ORDER:
        return "%d lots exceeds the %d-lot ceiling" % (
            g.intent.lots, contract.MAX_LOTS_PER_ORDER)
    return None


def _notional_cap(g: GuardInput):
    cap = contract.MAX_NOTIONAL_PER_ORDER_RS
    if cap > 0 and g.intent.notional_rs > cap:
        return "notional Rs%.0f exceeds the Rs%.0f per-order cap" % (
            g.intent.notional_rs, cap)
    return None


def _loss_cap(g: GuardInput):
    cap = contract.DAILY_LOSS_CAP_RS
    if cap > 0 and g.day_pnl_rs <= -cap:
        return "day P&L Rs%.0f is at or past the Rs%.0f loss cap" % (
            g.day_pnl_rs, cap)
    return None


def _caps_unset(g: GuardInput):
    """Fail closed: live may not open while an account-sized cap is still 0."""
    if g.arm_mode != "live":
        return None
    missing = contract.unset_live_caps()
    if missing:
        return "live opens are blocked until these caps are set in contract.py: %s" % (
            ", ".join(missing))
    return None


def _reconcile_stale(g: GuardInput):
    if g.arm_mode != "live":
        return None
    if g.reconciled_at is None:
        return "position book never reconciled against the broker"
    age = g.now - g.reconciled_at
    if age > contract.MAX_RECONCILE_AGE_S:
        return "position book last reconciled %.0fs ago (max %.0fs)" % (
            age, contract.MAX_RECONCILE_AGE_S)
    return None


# scope: "all" applies to every intent; "open" only to purpose == "open";
# "exit" only to close/flatten. Order here is the order of the report, so the
# first failing code is the most fundamental one.
CHECKS: Tuple[Tuple[str, str, Callable], ...] = (
    ("KILLED",               "all",  _killed),
    ("BAD_PURPOSE",          "all",  _bad_purpose),
    ("SEGMENT_NOT_ALLOWED",  "all",  _segment),
    ("ORDER_TYPE_NOT_ALLOWED", "all", _order_type),
    ("BAD_QUANTITY",         "all",  _quantity),
    ("BAD_PRICE",            "all",  _price),
    ("DUPLICATE",            "all",  _duplicate),
    ("TOKEN_INVALID",        "all",  _token),
    ("RATE_LIMITED",         "all",  _rate),
    ("DAY_ORDER_CAP",        "all",  _day_order_cap),
    ("EXIT_UNOWNED",         "exit", _exit_unowned),
    ("NOT_ARMED",            "open", _armed),
    ("ARM_EXPIRED",          "open", _arm_expired),
    ("MODE_UNKNOWN",         "open", _mode),
    ("CAPS_UNSET",           "open", _caps_unset),
    ("NOT_TRADING_DAY",      "open", _trading_day),
    ("OUTSIDE_WINDOW",       "open", _window),
    ("CAS_FREEZE",           "open", _cas),
    ("PAST_FLATTEN",         "open", _past_flatten),
    ("FEED_UNHEALTHY",       "open", _feed),
    ("TICK_STALE",           "open", _tick_stale),
    ("CLOCK_SKEW",           "open", _clock),
    ("OPEN_ORDER_CAP",       "open", _open_order_cap),
    ("POSITION_CAP",         "open", _position_cap),
    ("LOT_CAP",              "open", _lot_cap),
    ("NOTIONAL_CAP",         "open", _notional_cap),
    ("DAILY_LOSS_CAP",       "open", _loss_cap),
    ("STALE_RECONCILE",      "open", _reconcile_stale),
)


def _in_scope(scope: str, intent: OrderIntent) -> bool:
    if scope == "all":
        return True
    return intent.is_exit if scope == "exit" else not intent.is_exit


def evaluate(g: GuardInput) -> GuardVerdict:
    """Every applicable check. First failure names the verdict; all are listed."""
    blocks = []
    first_reason = ""
    for code, scope, fn in CHECKS:
        if not _in_scope(scope, g.intent):
            continue
        reason = fn(g)
        if reason:
            if not blocks:
                first_reason = reason
            blocks.append(code)
    if blocks:
        return GuardVerdict(allowed=False, code=blocks[0], reason=first_reason,
                            blocks=tuple(blocks))
    return GuardVerdict(allowed=True, code="OK", reason="", blocks=())


def check_names() -> Tuple[Tuple[str, str], ...]:
    """(code, scope) for every check — the UI's rails panel renders this."""
    return tuple((code, scope) for code, scope, _ in CHECKS)
