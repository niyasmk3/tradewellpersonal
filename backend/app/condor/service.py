"""Condor loop orchestration: evaluate → reconcile → trace → snapshot.

Runs at CONDOR_EVAL_S (60s default — condor conditions move at regime speed,
not tick speed). Shadow-first: cards land in the store/archive and render on
the /condor tab labelled TRIAL; nothing is pushed to the phone until the
ledger has its 30 graded outcomes and the user flips that switch deliberately.
"""
from __future__ import annotations

import logging
import time

from app.condor.engine import CondorEngine
from app.condor.models import CondorCard, WhatIfRequest
from app.condor.monitor import CondorMonitor
from app.condor.snapshots import SnapshotLogger
from app.condor.store import CondorArchive, CondorStore, EvalTrace
from app.kite.instruments import chain_key
from app.market import calendar as mcal
from app.options.iv import bs_price
from app.state import MarketState

log = logging.getLogger("tradewell.condor")


class CondorService:
    def __init__(self, cfg, state: MarketState, universes: dict,
                 store: CondorStore, trace: EvalTrace,
                 snapshots: SnapshotLogger | None = None) -> None:
        self.cfg = cfg
        self.state = state
        self.universes = universes
        self.store = store
        self.trace = trace
        self.snapshots = snapshots
        self.engine = CondorEngine(cfg, state)
        self.monitor = CondorMonitor(cfg, state, store)
        self.last_responses: dict[str, dict] = {}

    def universe_for(self, symbol: str):
        return self.universes.get(chain_key(symbol, "nearest"))

    def run_once(self) -> None:
        cfg = self.cfg
        now = time.time()
        if self.snapshots is not None and mcal.is_market_open():
            n = self.snapshots.maybe_snapshot(
                self.universes, cfg.condor_snapshot_s,
                cfg.condor_snapshot_keep_days, now)
            if n:
                log.debug("condor snapshot: %d rows", n)
        for symbol in cfg.condor_symbol_list:
            try:
                uni = self.universe_for(symbol)
                if uni is None:
                    continue
                chain = self.state.get_option_chain(chain_key(symbol, "nearest"))
                resp = self.engine.evaluate(symbol, uni, chain, now)
                # Open-position cap is a gate too (spec G6).
                if resp.card is not None and \
                        len(self.store.open_positions()) >= cfg.condor_max_open:
                    resp.no_trade_reasons = [
                        f"max open condors ({cfg.condor_max_open}) reached"]
                    resp.card = None
                self.store.reconcile(symbol, resp.card, now)
                self.trace.record(resp)
                self.last_responses[symbol] = resp.model_dump()
            except Exception as exc:
                log.warning("condor evaluation failed for %s: %s", symbol, exc)

    # ------------------------------------------------------------------
    def position_pseudo_card(self, pos) -> CondorCard | None:
        """A pseudo-card for scenario-pricing a journaled position, legs
        enriched with LIVE solved IVs from the position's own universe.
        None when the legs cannot be priced honestly (wrong-expiry universe,
        stale quotes, unsolvable IV) — fail closed rather than assume a vol."""
        import time as _t

        from app.condor.chain_view import ChainView
        from app.condor.monitor import CondorMonitor

        uni = CondorMonitor._universe_for(pos, self.universes)
        snap = self.state.underlying_snapshot(pos.symbol)
        spot = snap.ltp if snap else None
        if uni is None or not spot:
            return None
        view = ChainView(self.state, uni, self.cfg.signal_max_premium_age_s)
        now = _t.time()
        legs = []
        for leg in pos.legs:
            q = view.leg(leg.strike, leg.right, spot, now)
            if q.iv is None:
                return None
            enriched = leg.model_copy(deep=True)
            enriched.iv = q.iv
            legs.append(enriched)
        return CondorCard(
            id=pos.id, symbol=pos.symbol, expiry=pos.expiry, spot=spot,
            legs=legs, width=pos.width, lot_size=pos.lot_size,
            lots=pos.lots, credit_mid=pos.credit_fill)

    def whatif(self, card: CondorCard, req: WhatIfRequest) -> dict:
        """Scenario grid: reprice each leg with BS at shifted spot/time/IV.
        Pure function of the card — no market calls, no LLM."""
        spot0 = card.spot or 0.0
        em = card.em_primary or (spot0 * 0.01)
        spots = sorted(set(
            [round(spot0 + k * em, 0) for k in (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5)]
            + [s for s in req.spots if s > 0]))
        qty = card.lot_size * card.lots
        t0 = _t_years_from_iso(card.expiry)
        rows = []
        for days in req.days_forward + ["expiry"]:
            t = 0.0 if days == "expiry" else max(t0 - float(days) / 365.0, 0.0)
            for shift in req.iv_shifts:
                cells = []
                for s in spots:
                    value = 0.0
                    for leg in card.legs:
                        # No fabricated fallback vol: a leg without a solved
                        # IV must be enriched by the caller (position path:
                        # position_pseudo_card) or the request refused. A
                        # grid silently priced at an assumed 15% was rupees-
                        # wrong on every cell (review C1).
                        if not leg.iv:
                            raise ValueError(
                                f"leg {leg.strike:.0f}{leg.right} has no IV — "
                                "cannot price scenarios")
                        sigma = leg.iv * (1.0 + shift)
                        px = bs_price(s, leg.strike, t, sigma, leg.right == "CE")
                        value += (-px if leg.side.value == "SELL" else px)
                    # value is negative when structure still holds credit value;
                    # cost to close = -value.
                    close_debit = max(0.0, -value) if t > 0 else _intrinsic(card, s)
                    pnl = round((card.credit_mid - close_debit) * qty, 0)
                    cells.append({"spot": s, "pnl": pnl})
                rows.append({"days": days, "iv_shift": shift, "cells": cells})
        return {"spots": spots, "rows": rows}


def _intrinsic(card: CondorCard, s: float) -> float:
    debit = 0.0
    for leg in card.legs:
        val = max(s - leg.strike, 0.0) if leg.right == "CE" else max(leg.strike - s, 0.0)
        debit += (val if leg.side.value == "SELL" else -val)
    return max(0.0, debit)


def _t_years_from_iso(expiry_iso: str | None) -> float:
    if not expiry_iso:
        return 0.0
    try:
        from datetime import date
        from app.options.iv import years_to_expiry
        y, m, d = (int(x) for x in expiry_iso.split("-"))
        return years_to_expiry(date(y, m, d))
    except Exception:
        return 0.0
