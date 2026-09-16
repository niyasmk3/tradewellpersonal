"""The pre-trade gate (16-Sep): the module's load-bearing test file.

The properties pinned here are the ones that decide whether an automated
account survives its own bugs:

  * a clean open passes, and a DEFAULT GuardInput does not — the gate fails
    closed, so a caller who forgets to fill in the world gets nothing;
  * exits are NOT subject to the discretionary checks, because a gate that
    refuses to let you out is worse than no gate;
  * the day's order budget reserves headroom for exits, so a day cannot spend
    itself into a position it cannot pay to leave;
  * the kill switch stops exits too — deliberate, and pinned so nobody
    "fixes" it later without reading why;
  * fail-closed caps: live may not open while an account-sized cap is unset.

Run:  python backend/tests/test_algo_guard.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.algo import contract
from app.algo.guard import GuardInput, evaluate
from app.algo.intent import OrderIntent, intent_key

NOW = 1_000_000.0
DAY = "2026-09-16"
MIDDAY = 12 * 60          # 12:00 IST, comfortably inside the entry window


def _intent(purpose="open", **over):
    kw = dict(
        key=intent_key("closing", DAY, over.pop("leg", "entry_ce"), purpose),
        strategy="closing", day=DAY, leg="entry_ce", purpose=purpose,
        segment="NFO", tradingsymbol="NIFTY26SEP25000CE", side="BUY",
        order_type="LIMIT", quantity=75, lots=1, price=120.50,
    )
    kw.update(over)
    return OrderIntent(**kw)


def _clean(purpose="open", **over):
    """A GuardInput that passes every check — the baseline each test bends."""
    kw = dict(
        intent=_intent(purpose), now=NOW, ist_minute=MIDDAY, trading_day=True,
        arm_strategy="closing", arm_mode="dry", arm_expires_at=NOW + 600,
        exit_mode="dry" if purpose in ("close", "flatten") else None,
        token_state="valid", feed_healthy=True, last_tick_age_s=2.0,
        clock_skew_s=0.1,
    )
    kw.update(over)
    return GuardInput(**kw)


# --- baseline -----------------------------------------------------------------

def test_clean_open_passes_and_bare_input_fails_closed():
    assert evaluate(_clean()).allowed is True
    bare = GuardInput(intent=_intent(), now=NOW, ist_minute=MIDDAY)
    v = evaluate(bare)
    assert v.allowed is False
    # Unset world => unknown token, no arm, dead feed, no tick, unknown clock.
    for code in ("TOKEN_INVALID", "NOT_ARMED", "FEED_UNHEALTHY", "TICK_STALE",
                 "CLOCK_SKEW"):
        assert code in v.blocks, (code, v.blocks)
    print("  GUARD  -> clean open passes; a bare GuardInput fails closed")


def test_every_block_is_reported_not_just_the_first():
    v = evaluate(_clean(token_state="invalid", feed_healthy=False,
                        last_tick_age_s=900.0))
    assert v.code == "TOKEN_INVALID"                    # first = most fundamental
    assert set(v.blocks) >= {"TOKEN_INVALID", "FEED_UNHEALTHY", "TICK_STALE"}
    print("  GUARD  -> all failing checks reported (%d), first names the verdict"
          % len(v.blocks))


# --- the open/exit split ------------------------------------------------------

def test_exits_bypass_the_discretionary_checks():
    """Outside the window, past flatten, dead feed, every cap breached, a
    losing day — an exit still gets through. This is the whole point."""
    g = _clean("close", ist_minute=15 * 60 + 25, trading_day=False,
               arm_strategy=None, arm_mode=None, arm_expires_at=None,
               feed_healthy=False, last_tick_age_s=None, clock_skew_s=None,
               open_positions=99, open_orders_today=99, day_pnl_rs=-9_999_999.0)
    v = evaluate(g)
    assert v.allowed is True, v.blocks
    print("  GUARD  -> exit passes with every open-only check violated")


def test_open_is_blocked_by_each_discretionary_check():
    cases = [
        ("NOT_TRADING_DAY",  dict(trading_day=False)),
        ("OUTSIDE_WINDOW",   dict(ist_minute=9 * 60)),
        ("PAST_FLATTEN",     dict(ist_minute=contract.HARD_FLATTEN_MIN + 1)),
        ("FEED_UNHEALTHY",   dict(feed_healthy=False)),
        ("TICK_STALE",       dict(last_tick_age_s=contract.MAX_TICK_AGE_S + 1)),
        ("CLOCK_SKEW",       dict(clock_skew_s=contract.MAX_CLOCK_SKEW_S + 1)),
        ("POSITION_CAP",     dict(open_positions=contract.MAX_OPEN_POSITIONS)),
        ("OPEN_ORDER_CAP",   dict(open_orders_today=contract.MAX_OPEN_ORDERS_PER_DAY)),
        ("NOT_ARMED",        dict(arm_strategy=None)),
        ("ARM_EXPIRED",      dict(arm_expires_at=NOW - 1)),
    ]
    for code, over in cases:
        v = evaluate(_clean(**over))
        assert not v.allowed and code in v.blocks, (code, v.blocks)
    print("  GUARD  -> each of %d discretionary checks blocks an open" % len(cases))


def test_exit_needs_a_recorded_open():
    v = evaluate(_clean("close", exit_mode=None))
    assert not v.allowed and v.code == "EXIT_UNOWNED", v.blocks
    print("  GUARD  -> exit with no recorded open is refused (EXIT_UNOWNED)")


# --- the always-checks reach exits too ----------------------------------------

def test_kill_switch_stops_exits_as_well():
    """Deliberate: a tripped switch hands the account to the human, whole. If
    this test is ever 'fixed', read killswitch.py's docstring first."""
    for purpose in ("open", "close", "flatten"):
        v = evaluate(_clean(purpose, kill_code="RECONCILE_DRIFT"))
        assert not v.allowed and v.code == "KILLED", (purpose, v.blocks)
    print("  GUARD  -> kill switch blocks opens AND exits (by design)")


def test_duplicate_key_is_refused_for_both_directions():
    for purpose in ("open", "close"):
        i = _intent(purpose)
        v = evaluate(_clean(purpose, seen_keys=frozenset({i.key})))
        assert not v.allowed and "DUPLICATE" in v.blocks, (purpose, v.blocks)
    print("  GUARD  -> a spent idempotency key blocks the repeat, either way")


def test_token_must_be_probed_valid():
    for st in ("invalid", "unknown", ""):
        v = evaluate(_clean(token_state=st))
        assert not v.allowed and "TOKEN_INVALID" in v.blocks, st
    print("  GUARD  -> only a probed 'valid' token may place (16-Sep lesson)")


def test_rate_limiter_counts_the_trailing_window_only():
    at_cap = tuple(NOW - 0.1 for _ in range(contract.MAX_ORDERS_PER_SEC))
    assert "RATE_LIMITED" in evaluate(_clean(recent_order_ts=at_cap)).blocks
    # The same orders, now older than the window, must not block.
    stale = tuple(NOW - contract.RATE_WINDOW_S - 0.1 for _ in at_cap)
    assert evaluate(_clean(recent_order_ts=stale)).allowed is True
    print("  GUARD  -> %d orders/s caps; orders older than %.0fs do not count"
          % (contract.MAX_ORDERS_PER_SEC, contract.RATE_WINDOW_S))


# --- the order budget ---------------------------------------------------------

def test_order_budget_reserves_headroom_for_exits():
    spent = contract.MAX_OPEN_ORDERS_PER_DAY
    g_open = _clean(open_orders_today=spent, orders_today=spent)
    assert "OPEN_ORDER_CAP" in evaluate(g_open).blocks
    g_exit = _clean("close", open_orders_today=spent, orders_today=spent)
    assert evaluate(g_exit).allowed is True, evaluate(g_exit).blocks
    # ...until the HARD ceiling, which counts exits too and stops everything.
    hard = _clean("close", orders_today=contract.MAX_ORDERS_PER_DAY)
    assert "DAY_ORDER_CAP" in evaluate(hard).blocks
    print("  GUARD  -> entries stop at %d, exits keep %d in reserve, ceiling %d"
          % (contract.MAX_OPEN_ORDERS_PER_DAY, contract.EXIT_RESERVE_ORDERS,
             contract.MAX_ORDERS_PER_DAY))


# --- malformed orders ---------------------------------------------------------

def test_price_must_be_tick_aligned():
    # 71.60 is an exact tick multiple that naive float maths calls unaligned.
    assert evaluate(_clean(intent=_intent(price=71.60))).allowed is True
    v = evaluate(_clean(intent=_intent(price=71.62)))
    assert not v.allowed and "BAD_PRICE" in v.blocks
    for bad in (None, 0.0, -5.0):
        assert "BAD_PRICE" in evaluate(_clean(intent=_intent(price=bad))).blocks
    print("  GUARD  -> 71.60 aligned, 71.62 refused, missing/negative refused")


def test_instrument_and_size_allowlists():
    assert "SEGMENT_NOT_ALLOWED" in evaluate(
        _clean(intent=_intent(segment="MCX"))).blocks
    assert "ORDER_TYPE_NOT_ALLOWED" in evaluate(
        _clean(intent=_intent(order_type="MARKET"))).blocks
    assert "LOT_CAP" in evaluate(
        _clean(intent=_intent(lots=contract.MAX_LOTS_PER_ORDER + 1))).blocks
    assert "BAD_QUANTITY" in evaluate(_clean(intent=_intent(quantity=0))).blocks
    print("  GUARD  -> segment / order-type / lot / quantity allowlists hold")


def test_arm_authorises_exactly_one_strategy():
    v = evaluate(_clean(arm_strategy="gold"))
    assert not v.allowed and "NOT_ARMED" in v.blocks
    print("  GUARD  -> arming gold does not authorise a closing intent")


def test_cas_freeze_applies_only_to_index_linked_intents():
    at_cas = contract.CAS_FREEZE_MIN[0] + 2
    # Past the flatten clock too, so isolate on the CAS code itself.
    assert "CAS_FREEZE" in evaluate(_clean(ist_minute=at_cas)).blocks
    not_index = _clean(ist_minute=at_cas, intent=_intent(index_linked=False))
    assert "CAS_FREEZE" not in evaluate(not_index).blocks
    print("  GUARD  -> CAS freeze blocks index-linked opens only")


# --- fail-closed caps ---------------------------------------------------------

def test_live_opens_blocked_while_account_caps_are_unset():
    """contract.py ships the two account-sized caps at 0 = UNSET. Unset must
    mean 'no live opens', never 'no limit'."""
    assert contract.unset_live_caps(), "caps are set — update this test with them"
    v = evaluate(_clean(arm_mode="live"))
    assert not v.allowed and "CAPS_UNSET" in v.blocks, v.blocks
    # The same intent in dry-run is fine: the ladder's lower rungs must run.
    assert evaluate(_clean(arm_mode="dry")).allowed is True
    print("  GUARD  -> unset caps block LIVE opens, dry/paper unaffected")


def test_live_also_requires_a_fresh_reconcile():
    stale = _clean(arm_mode="live", reconciled_at=NOW - contract.MAX_RECONCILE_AGE_S - 1)
    assert "STALE_RECONCILE" in evaluate(stale).blocks
    assert "STALE_RECONCILE" not in evaluate(_clean(arm_mode="dry")).blocks
    print("  GUARD  -> live needs a reconcile inside %.0fs; dry does not"
          % contract.MAX_RECONCILE_AGE_S)


def test_daily_loss_cap_halts_opens_when_set():
    """The cap ships unset, so drive the check through a temporary value —
    the constant itself stays frozen."""
    saved = contract.DAILY_LOSS_CAP_RS
    try:
        contract.DAILY_LOSS_CAP_RS = 5000.0
        assert "DAILY_LOSS_CAP" in evaluate(_clean(day_pnl_rs=-5000.0)).blocks
        assert evaluate(_clean(day_pnl_rs=-4999.0)).allowed is True
        # and it must never strand an exit
        assert evaluate(_clean("close", day_pnl_rs=-50_000.0)).allowed is True
    finally:
        contract.DAILY_LOSS_CAP_RS = saved
    print("  GUARD  -> loss cap halts opens at the threshold, never exits")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
