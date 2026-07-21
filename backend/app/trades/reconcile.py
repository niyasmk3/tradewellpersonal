"""Reconcile the journal against Zerodha's actual position book.

WHY THIS EXISTS: Tradewell places no orders, so until now it could only INFER
whether you still held a position — from a price crossing a level. That
inference is wrong in both directions. It closes a row you are still holding
(the stop printed but you did not act), and it leaves a row open that Kite has
already squared off. Both corrupt realised P&L and the circuit breakers that
read it.

Reading `positions()` is a READ-ONLY call and needs no order permission. It
turns the journal from a guess into a mirror:

  * broker flat, journal open   -> you exited (or MIS was auto-squared): close
    the row, and at Kite's OWN average sell price when it reports one, so the
    P&L is your real fill rather than an estimate.
  * broker holding, row auto-closed -> the price-based auto-close was wrong:
    reopen it. The broker is the authority, not the tape.

A failed API call must NEVER be read as "no positions" — that would close every
open row at once. `fetch_broker_positions` returns None on failure and the
caller does nothing, falling back to price-based auto-close.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("tradewell.trades")


@dataclass
class BrokerPosition:
    tradingsymbol: str
    token: Optional[int]
    product: str
    quantity: int                    # net open qty; 0 means flat
    sell_quantity: int = 0
    sell_price: Optional[float] = None


def _row(raw: dict) -> BrokerPosition:
    return BrokerPosition(
        tradingsymbol=str(raw.get("tradingsymbol") or ""),
        token=int(raw["instrument_token"]) if raw.get("instrument_token") else None,
        product=str(raw.get("product") or ""),
        quantity=int(raw.get("quantity") or 0),
        sell_quantity=int(raw.get("sell_quantity") or 0),
        sell_price=float(raw["sell_price"]) if raw.get("sell_price") else None,
    )


def fetch_broker_positions(kite) -> Optional[list[BrokerPosition]]:
    """Every position row Kite reports, or None if the call failed.

    None vs [] is the whole safety property: [] means "you hold nothing", None
    means "we do not know". Only the first may close a journal row.
    """
    if kite is None:
        return None
    try:
        data = kite.positions() or {}
    except Exception as exc:
        log.warning("position book unavailable (%s) — skipping reconciliation", exc)
        return None
    if not isinstance(data, dict):
        return None
    # `net` carries the running position; `day` carries today's activity, and is
    # the one that reports a sell price after an intraday square-off. Prefer a
    # `net` row for quantity and fall back to `day` for the fill detail.
    rows: list[BrokerPosition] = []
    for bucket in ("net", "day"):
        for raw in data.get(bucket) or []:
            try:
                rows.append(_row(raw))
            except Exception:      # one malformed row must not blind the rest
                log.debug("unparseable position row skipped", exc_info=True)
    return rows


def match(trade, positions: list[BrokerPosition]) -> Optional[BrokerPosition]:
    """The broker row for this trade, preferring an exact instrument match.

    Token first: a tradingsymbol can repeat across products, and the token
    identifies exactly one contract. Product is checked when the trade records
    one, so an MIS long is never reconciled against an NRML row.
    """
    want_product = (getattr(trade, "product", None) or "").upper()

    def ok(p: BrokerPosition) -> bool:
        return not want_product or not p.product or p.product.upper() == want_product

    if trade.token is not None:
        exact = [p for p in positions if p.token == trade.token and ok(p)]
        if exact:
            # A holding row beats a flat one when both exist (net + day).
            return next((p for p in exact if p.quantity != 0), exact[0])
    tsym = (trade.contract or "").strip()
    named = [p for p in positions if p.tradingsymbol == tsym and ok(p)]
    if named:
        return next((p for p in named if p.quantity != 0), named[0])
    return None
