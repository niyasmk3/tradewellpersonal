"""The frozen execution contract — every limit that stands between a bug and
the account, as constants in code.

WHY NOT config.py. Same reasoning as gold/rules.py: an .env-tunable limit is
an editable one, and a limit edited in the middle of a drawdown is not a
limit. Changing anything here is a commit, with a date and a diff, reviewed
while nothing is on fire. The runtime-editable settings in risk_limits.py are
deliberately NOT reused — those are conveniences (today's fund, the paper
simulator's position cap) and they are editable from the UI, which is exactly
what these must never be.

FAIL-CLOSED CAPS. The two account-sized limits (daily loss, per-order
notional) ship as 0.0, meaning UNSET. Unset does not mean unlimited — it
blocks every live open until a human writes a real number here. A risk cap
whose default is "no cap" has the failure mode backwards.

HEADROOM, NOT CEILING. MAX_ORDERS_PER_SEC is 2 against a regulatory ceiling
of 10 (SEBI retail algo framework, mandatory 01-Apr-2026: above 10 OPS per
segment per exchange a strategy needs exchange registration and an
exchange-issued Algo ID). Two orders a second is an order of magnitude of
slack, so no plausible runaway loop can push this account into territory it
was never registered for.
"""
from __future__ import annotations

from datetime import date

# Contract written and frozen on this date. A limit that moves gets a new
# date here and a line in the plan doc's changelog.
FREEZE_DATE = date(2026, 9, 16)

# --- rate ---------------------------------------------------------------------
MAX_ORDERS_PER_SEC = 2          # regulatory ceiling is 10; we take a tenth of it
RATE_WINDOW_S = 1.0

# --- volume -------------------------------------------------------------------
# The hard ceiling counts EVERY order, entries and exits alike. The open cap
# below it reserves headroom so a day that spends its budget on entries can
# still pay to get out — an order budget that can strand a position is a bug
# dressed as a risk control.
MAX_ORDERS_PER_DAY = 20
EXIT_RESERVE_ORDERS = 6
MAX_OPEN_ORDERS_PER_DAY = MAX_ORDERS_PER_DAY - EXIT_RESERVE_ORDERS

MAX_OPEN_POSITIONS = 2
MAX_LOTS_PER_ORDER = 1          # raised by promotion (A4), never by a knob

# --- money (UNSET = fail closed; see the module docstring) --------------------
MAX_NOTIONAL_PER_ORDER_RS = 0.0
DAILY_LOSS_CAP_RS = 0.0         # positive rupees; day P&L at or below -this halts

# --- instrument surface -------------------------------------------------------
ALLOWED_SEGMENTS = frozenset({"NFO"})
ALLOWED_ORDER_TYPES = frozenset({"LIMIT"})   # MARKET arrives later, if ever
TICK_SIZE = 0.05

# --- clock (minutes since IST midnight) ---------------------------------------
# Opens may be placed inside this window only. It closes at 15:12 rather than
# a rounder 15:00 because the first strategy this module serves (Closing Day)
# fills at the 15:05 settle clock and needs a few minutes of retry room —
# tests/test_algo_closing_adapter.py pins that the adapter's window fits
# inside this one, so a future edit here cannot silently starve it.
ENTRY_WINDOW_MIN = (9 * 60 + 20, 15 * 60 + 12)
# No OPEN at or after this minute, whatever the product. (Zerodha RMS squares
# MIS positions off from ~15:20; an NRML overnight leg is the strategy's own
# business, but nothing new starts this close to the bell either way.)
HARD_FLATTEN_MIN = 15 * 60 + 15
# NSE Closing Auction Session (live since 03-Aug-2026): the index print is
# frozen through this window, so anything priced off it is priced off a lie.
CAS_FREEZE_MIN = (15 * 60 + 15, 15 * 60 + 35)

# --- staleness ----------------------------------------------------------------
MAX_TICK_AGE_S = 15.0
# Skew is measured as wall-clock-at-receipt minus the tick's exchange
# timestamp, which Kite truncates to whole seconds; ~1s of that plus
# network latency is normal, so 2s would page on a healthy day.
MAX_CLOCK_SKEW_S = 3.0
MAX_RECONCILE_AGE_S = 120.0

# --- arming -------------------------------------------------------------------
ARM_TTL_S = 3600.0              # a live arm expires by itself, always
CONSECUTIVE_REJECTS_KILL = 3    # broker rejections in a row -> kill switch

# The rungs of the rollout ladder. `live` is not reachable until a strategy
# clears its own evidence bar AND a human edits the caps above.
MODES = ("dry", "paper", "live")


def unset_live_caps() -> tuple:
    """Which fail-closed caps are still unset — the preflight's blocking list."""
    missing = []
    if MAX_NOTIONAL_PER_ORDER_RS <= 0:
        missing.append("MAX_NOTIONAL_PER_ORDER_RS")
    if DAILY_LOSS_CAP_RS <= 0:
        missing.append("DAILY_LOSS_CAP_RS")
    return tuple(missing)


def snap_to_tick(price: float, up: bool) -> float:
    """Snap to the ₹0.05 tick, rounding `up` or down. Same quotient-rounding
    guard as is_tick_aligned — an exact multiple must not lose a tick."""
    import math
    q = round(price / TICK_SIZE, 6)
    n = math.ceil(q) if up else math.floor(q)
    return round(n * TICK_SIZE, 2)


def is_tick_aligned(price: float) -> bool:
    """Is `price` a whole multiple of the ₹0.05 tick?

    Rounds the quotient before testing, for the same reason routes_kite_basket
    does: 71.60 / 0.05 == 1431.9999999999998, so a naive modulo calls an
    already-aligned price unaligned about a third of the time.
    """
    q = round(price / TICK_SIZE, 6)
    return abs(q - round(q)) < 1e-9
