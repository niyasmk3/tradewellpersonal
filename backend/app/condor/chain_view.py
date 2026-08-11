"""The condor's read of the live option tape — per-leg quotes that FAIL CLOSED.

The production chain (options/chain.py) republishes whatever is in the tick
cache and lets downstream guards decide; its spread check (signals/strike.py)
passes when depth is missing. That is survivable for a bought ATM option and
not for a written condor leg, so this view inverts the default: a leg without
a fresh exchange-stamped tick, a live bid AND ask, and a positive LTP is not
tradeable, and every refusal carries its reason.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from app.condor.greeks import bs_greeks, solve_iv
from app.kite.instruments import OptionUniverse
from app.options.iv import years_to_expiry
from app.state import MarketState


@dataclass
class LegQuote:
    strike: float
    right: str                       # "CE" | "PE"
    token: int | None = None
    tradingsymbol: str | None = None
    lot_size: int = 0
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread: float | None = None      # ask - bid, rupees
    spread_pct: float | None = None  # spread / mid
    oi: float | None = None
    volume: float | None = None
    age_s: int | None = None         # vs the tick's own exchange timestamp
    iv: float | None = None          # FRACTION (0.143)
    greeks: dict | None = None       # delta/gamma/theta/vega at `iv`
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class ChainView:
    """Quotes any strike in a subscribed universe straight from the tick cache."""

    def __init__(self, state: MarketState, universe: OptionUniverse,
                 max_age_s: int = 120) -> None:
        self.state = state
        self.universe = universe
        self.max_age_s = max_age_s

    @property
    def expiry(self) -> date | None:
        return self.universe.expiry

    def t_years(self, now_ts: float | None = None) -> float:
        return years_to_expiry(self.universe.expiry, now_ts)

    def leg(self, strike: float, right: str, spot: float | None,
            now_ts: float | None = None) -> LegQuote:
        now = time.time() if now_ts is None else now_ts
        q = LegQuote(strike=strike, right=right)
        pair = self.universe.strikes.get(strike)
        if pair is None:
            q.problems.append(f"strike {strike:.0f} not in subscribed universe")
            return q
        is_call = right == "CE"
        q.token = pair.ce_token if is_call else pair.pe_token
        q.tradingsymbol = pair.ce_symbol if is_call else pair.pe_symbol
        q.lot_size = pair.ce_lot_size if is_call else pair.pe_lot_size
        if not q.token:
            q.problems.append("no instrument token")
            return q

        tick = self.state.ticks.get(q.token)
        if not tick:
            q.problems.append("no tick yet")
            return q
        ts = tick.get("ts")
        if not ts:
            q.problems.append("tick has no exchange timestamp")
        else:
            q.age_s = int(now - ts)
            if q.age_s > self.max_age_s:
                q.problems.append(f"quote stale ({q.age_s}s)")

        q.ltp = tick.get("last_price")
        if not q.ltp or q.ltp <= 0:
            q.problems.append("LTP missing/zero")
        q.oi = tick.get("oi")
        q.volume = tick.get("volume_traded")

        depth = tick.get("depth") or {}
        try:
            q.bid = (depth.get("buy") or [{}])[0].get("price") or None
            q.ask = (depth.get("sell") or [{}])[0].get("price") or None
        except (IndexError, AttributeError, TypeError):
            q.bid = q.ask = None
        if q.bid and q.ask and q.ask >= q.bid > 0:
            q.mid = round((q.bid + q.ask) / 2.0, 2)
            q.spread = round(q.ask - q.bid, 2)
            q.spread_pct = round(q.spread / q.mid, 4) if q.mid else None
        else:
            # Missing depth fails CLOSED here — the exact inversion of
            # signals/strike._spread_ok, on purpose (a written leg you cannot
            # price the exit of is not a trade, it's a hope).
            q.problems.append("no live bid/ask depth")

        # IV + Greeks off the mid where we have one, else LTP. Solved for any
        # strike (see greeks.solve_iv) — the solver's own refusals gate quality.
        ref = q.mid or q.ltp
        t = self.t_years(now)
        if ref and spot and t > 0:
            q.iv = solve_iv(ref, spot, strike, t, is_call)
            if q.iv is None:
                q.problems.append("IV unsolvable (stale/degenerate premium)")
            elif not 0.05 <= q.iv <= 0.60:
                q.problems.append(f"IV {q.iv * 100:.0f}% outside sane band")
            else:
                q.greeks = bs_greeks(spot, strike, t, q.iv, is_call)
        return q

    def strikes(self) -> list[float]:
        return sorted(self.universe.strikes)
