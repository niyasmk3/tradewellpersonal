"""Closing Day adapter — the 15:05 decision card, as two orders a day.

WHAT IT READS. The decision-time log (.closing_tonight.jsonl) — the FIRST
settled evaluation of each date, the exact card the human saw at 15:05, never
a re-read. That row already carries everything an order needs: direction
(CE/PE from the day body), the ATM strike, the holdable expiry, the verdict.
The adapter adds no rule and reads no tape; if the card is not logged yet
this tick does nothing and the next one looks again.

WHAT IT DOES. Entry window 15:05-15:12: BUY one lot of the card's contract
at a LIMIT just above the tape, product NRML (it is held overnight). Exit
window 09:50-10:05 on any later trading day: SELL every open position this
strategy holds, LIMIT just below the tape. Both legs are what study.py has
graded for a year — the 15:05 fill and the 09:50 print — so the dry-run
intent stream diffs directly against the ledger's modelled and real legs.

ENTRY POLICY is a frozen constant, not a knob: "clean" fires only on a CLEAN
verdict (what the card presents as go), "all" is the unfiltered rule. The
22-Aug filter hunt found every filter dead — so this choice is a hypothesis
the dry-run ledger will help price, not a setting to fiddle with. Nights the
policy skips are LOGGED as skips: an A0 ledger that only shows what fired
cannot tell you what it declined.

ONE LOT. contract.MAX_LOTS_PER_ORDER caps it anyway; this adapter does not
even ask for more.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from app.algo import contract
from app.algo.intent import OrderIntent, intent_key

log = logging.getLogger("tradewell.algo")

STRATEGY = "closing"
LEG = "overnight"
ENTRY_POLICY = "clean"                  # "clean" | "all"  — frozen, see docstring
ENTRY_WINDOW_MIN = (15 * 60 + 5, 15 * 60 + 12)
EXIT_WINDOW_MIN = (9 * 60 + 50, 10 * 60 + 5)
LIMIT_BUFFER_PCT = 0.005                # marketable limit: 0.5% through the tape
LOTS = 1
PRODUCT = "NRML"                        # held overnight — MIS would be squared off
# A blocked intent is retried on later ticks inside the window, but not every
# tick: an A0 ledger with 30 identical TOKEN_INVALID rows says nothing 3 rows
# would not.
RETRY_SPACING_S = 60.0


# --- pure: card -> decision -> intent ----------------------------------------

def entry_decision(card: Optional[dict]) -> tuple:
    """(go, reason). Reads the LOGGED card only — never a live re-evaluation."""
    if not card:
        return False, "no settled 15:05 card logged for today yet"
    v = card.get("values") or {}
    if not v.get("direction"):
        return False, "card has no direction (flat day body)"
    if not v.get("strike") or not v.get("expiry"):
        return False, "card has no strike/expiry"
    verdict = card.get("verdict")
    if ENTRY_POLICY == "clean" and verdict != "CLEAN":
        return False, "verdict %s — policy fires on CLEAN only (red: %s)" % (
            verdict, ", ".join(card.get("red") or []) or "none")
    return True, "verdict %s, %s %s exp %s" % (
        verdict, v["direction"], v["strike"], v["expiry"])


def build_entry(card: dict, tradingsymbol: str, lot_size: int, ltp: float,
                day: str) -> OrderIntent:
    v = card["values"]
    price = contract.snap_to_tick(ltp * (1.0 + LIMIT_BUFFER_PCT), up=True)
    return OrderIntent(
        key=intent_key(STRATEGY, day, LEG, "open"),
        strategy=STRATEGY, day=day, leg=LEG, purpose="open", segment="NFO",
        tradingsymbol=tradingsymbol, side="BUY", order_type="LIMIT",
        quantity=int(lot_size) * LOTS, lots=LOTS, price=price, index_linked=True,
        note="%s %s %s exp %s; tape %.2f; %s" % (
            card.get("verdict"), v["direction"], v["strike"], v["expiry"], ltp, PRODUCT),
    )


def build_exit(open_row: dict, ltp: float, day: str) -> OrderIntent:
    """Close the position an acked open row describes. `day` is the calendar
    day the exit is placed (budgets); the key carries the OPEN's day so the
    pair stays unique per position."""
    price = contract.snap_to_tick(ltp * (1.0 - LIMIT_BUFFER_PCT), up=False)
    return OrderIntent(
        key=intent_key(STRATEGY, open_row["day"], LEG, "close"),
        strategy=STRATEGY, day=day, leg=LEG, purpose="close", segment="NFO",
        tradingsymbol=open_row["tradingsymbol"], side="SELL", order_type="LIMIT",
        quantity=int(open_row["quantity"]), lots=int(open_row.get("lots") or LOTS),
        price=price, index_linked=True, closes=open_row["key"],
        note="exit of %s opened %s; tape %.2f%s" % (
            open_row["tradingsymbol"], open_row["day"], ltp,
            "" if open_row["day"] == _prev_trading_day_note(day) else " (LATE exit)"),
    )


def _prev_trading_day_note(day: str) -> str:
    # The exit is "on time" when its open was the previous trading session.
    # Resolved lazily so the pure builders stay importable without a calendar.
    from datetime import date, timedelta
    from app.market import calendar as mcal
    d = date.fromisoformat(day)
    for _ in range(10):
        d -= timedelta(days=1)
        if mcal.is_trading_day(d):
            return d.isoformat()
    return ""


def in_window(ist_minute: int, window: tuple) -> bool:
    return window[0] <= ist_minute <= window[1]


# --- live collaborators (each isolated so tests replace it) ------------------

class Resolved:
    __slots__ = ("tradingsymbol", "lot_size", "token")

    def __init__(self, tradingsymbol: str, lot_size: int, token: Optional[int]):
        self.tradingsymbol, self.lot_size, self.token = tradingsymbol, lot_size, token


def resolve_contract(card: dict) -> Optional[Resolved]:
    """The card's contract: the feed's live universe first (already
    subscribed, lot size per contract), then the NFO master by expiry/strike
    — the card's holdable expiry is not always the feed's nearest one."""
    from datetime import date

    from app.closing import realquote
    from app.kite.client import kite_service
    from app.kite.instruments import chain_key
    from app.services import feed

    v = card["values"]
    expiry = date.fromisoformat(v["expiry"])
    strike, kind = float(v["strike"]), v["direction"]
    builder = getattr(feed, "chain_builder", None)
    if builder is not None:
        uni = builder.universes.get(chain_key("NIFTY", "nearest"))
        if uni is not None and uni.expiry == expiry:
            sp = uni.strikes.get(strike)
            if sp is not None:
                tsym = sp.ce_symbol if kind == "CE" else sp.pe_symbol
                lot = sp.ce_lot_size if kind == "CE" else sp.pe_lot_size
                tok = sp.ce_token if kind == "CE" else sp.pe_token
                if tsym and lot:
                    return Resolved(tsym, int(lot), tok)
    if not kite_service.is_authenticated:
        return None
    nfo = realquote._nfo_instruments(kite_service.kite)
    for i in nfo:
        if (i.get("name") == "NIFTY" and i.get("instrument_type") == kind
                and realquote._as_date(i.get("expiry")) == expiry
                and float(i.get("strike") or 0) == strike):
            return Resolved(i["tradingsymbol"], int(i.get("lot_size") or 0),
                            int(i["instrument_token"]))
    return None


def quote(tradingsymbol: str, token: Optional[int]) -> Optional[float]:
    """Last price: the live tick if we hold one for the token, else one Kite
    LTP call. None when neither answers — the caller logs a skip and retries."""
    from app.kite.client import kite_service
    from app.state import market_state

    if token is not None:
        t = market_state.ticks.get(token)
        if t and t.get("last_price"):
            return float(t["last_price"])
    if not kite_service.is_authenticated:
        return None
    try:
        data = kite_service.kite.ltp(["NFO:" + tradingsymbol])
        row = data.get("NFO:" + tradingsymbol) or {}
        lp = row.get("last_price")
        return float(lp) if lp else None
    except Exception as exc:
        log.warning("algo closing: LTP for %s failed: %s", tradingsymbol, exc)
        return None


def logged_card(today) -> Optional[dict]:
    from app.closing import tonight
    return tonight._first_logged(today)


# --- the adapter ---------------------------------------------------------------

class ClosingAdapter:
    """One tick(now) per poll. Holds only throttle state; every fact about
    positions and spent keys comes from the ledger, so a restart mid-window
    picks up exactly where the file says."""

    def __init__(self, submit_fn: Callable, *, card_for=logged_card,
                 resolve=resolve_contract, quote_fn=quote) -> None:
        self._submit = submit_fn          # (intent, now, ist_minute) -> SubmitOutcome | None
        self._card_for = card_for
        self._resolve = resolve
        self._quote = quote_fn
        self._last_try: dict = {}         # key -> epoch of last attempt
        self._skipped: set = set()        # days whose skip was already logged
        self._done: set = set()           # keys placed (or found spent) — no more tries

    def _due(self, key: str, now_ts: float) -> bool:
        if key in self._done:
            return False
        last = self._last_try.get(key)
        return last is None or now_ts - last >= RETRY_SPACING_S

    def _settle(self, key: str, res) -> None:
        """A BLOCKED intent is retried on later ticks (the block may clear);
        a PLACED one is finished, and so is one the ledger already holds —
        retrying either only manufactures DUPLICATE rows."""
        if res is None:
            return
        code = getattr(getattr(res, "verdict", None), "code", None)
        if getattr(res, "placed", False) or code == "DUPLICATE":
            self._done.add(key)

    def tick(self, now: datetime, ledger, positions: list) -> list:
        """Returns the SubmitOutcomes produced (possibly empty). Never places
        anything itself — every intent goes through submit_fn's guard."""
        day = now.date().isoformat()
        ist_min = now.hour * 60 + now.minute
        now_ts = now.timestamp()
        out = []

        if in_window(ist_min, EXIT_WINDOW_MIN) and positions:
            for row in positions:
                key = intent_key(STRATEGY, row["day"], LEG, "close")
                if not self._due(key, now_ts):
                    continue
                ltp = self._quote(row["tradingsymbol"], row.get("token"))
                if ltp is None:
                    self._last_try[key] = now_ts
                    ledger.record("skip", day, {"key": key, "reason":
                                  "no quote for %s" % row["tradingsymbol"]}, now=now_ts)
                    continue
                self._last_try[key] = now_ts
                res = self._submit(build_exit(row, ltp, day), now_ts, ist_min)
                self._settle(key, res)
                if res is not None:
                    out.append(res)

        if in_window(ist_min, ENTRY_WINDOW_MIN):
            key = intent_key(STRATEGY, day, LEG, "open")
            if self._due(key, now_ts):
                card = self._card_for(now.date())
                go, why = entry_decision(card)
                if not go:
                    # A missing card is "not yet" (retry); a policy skip is
                    # final for the day and logged exactly once.
                    if card and day not in self._skipped:
                        self._skipped.add(day)
                        ledger.record("skip", day, {"key": key, "reason": why},
                                      now=now_ts)
                    if card:
                        self._last_try[key] = now_ts
                    return out
                self._last_try[key] = now_ts
                c = self._resolve(card)
                if c is None:
                    ledger.record("skip", day, {"key": key, "reason":
                                  "could not resolve contract for %s" % why}, now=now_ts)
                    return out
                ltp = self._quote(c.tradingsymbol, c.token)
                if ltp is None:
                    ledger.record("skip", day, {"key": key, "reason":
                                  "no quote for %s" % c.tradingsymbol}, now=now_ts)
                    return out
                res = self._submit(build_entry(card, c.tradingsymbol, c.lot_size, ltp, day),
                                   now_ts, ist_min)
                self._settle(key, res)
                if res is not None:
                    out.append(res)
        return out
