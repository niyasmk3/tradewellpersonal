"""Option-chain assembly from live ticks.

Because we subscribe option instruments in FULL mode, each tick already carries
``oi`` and ``volume_traded`` — so the chain is built by reading current state,
no extra quote polling required. We keep a per-token OI *baseline* (first value
seen this session) and report ``oi_change`` as the build since that baseline,
which is what surfaces intraday call/put writing.

The subscribed universe is intentionally wide; each poll we window it to
``display_depth`` strikes around the *live* ATM so the chain follows price
intraday instead of freezing on the open. The OI baseline is reset at the IST
session rollover so ``oi_change`` never drifts across days on a long-running
process.

Not provided by Kite ticks/quotes: implied volatility. IV is left ``None`` here;
computing it via Black-Scholes is a Phase-2 addition.
"""
from __future__ import annotations

import time

from app.kite.instruments import OptionUniverse
from app.models.schemas import OptionChain, OptionRow
from app.state import MarketState

_IST_OFFSET = 19800  # +5:30 in seconds


def _ist_day() -> int:
    return int((time.time() + _IST_OFFSET) // 86400)


class OptionChainBuilder:
    def __init__(self, state: MarketState, universes: dict[str, OptionUniverse], display_depth: int = 10) -> None:
        self.state = state
        # universes keyed by compound key, e.g. "NIFTY:nearest" / "NIFTY:monthly"
        self.universes = universes
        self.display_depth = display_depth
        self._session_day = _ist_day()

    def build(self, key: str) -> OptionChain | None:
        universe = self.universes.get(key)
        if universe is None:
            return None

        symbol = universe.symbol
        meta = self.state.underlyings.get(symbol.upper())
        spot_ltp = None
        if meta:
            spot_ltp = self.state.ticks.get(meta.spot_token, {}).get("last_price")
        atm = round(spot_ltp / universe.step) * universe.step if spot_ltp else None

        # Window the (wide) universe to display_depth strikes around the LIVE atm
        # so the chain follows price. Without a spot, centre on the universe.
        strikes = sorted(universe.strikes)
        if atm is not None:
            lo = atm - self.display_depth * universe.step
            hi = atm + self.display_depth * universe.step
            strikes = [s for s in strikes if lo <= s <= hi]

        rows: list[OptionRow] = []
        total_ce_oi = 0.0
        total_pe_oi = 0.0

        for strike in strikes:
            pair = universe.strikes[strike]
            ce = self.state.ticks.get(pair.ce_token, {}) if pair.ce_token else {}
            pe = self.state.ticks.get(pair.pe_token, {}) if pair.pe_token else {}

            ce_oi = ce.get("oi")
            pe_oi = pe.get("oi")
            total_ce_oi += ce_oi or 0
            total_pe_oi += pe_oi or 0

            rows.append(
                OptionRow(
                    strike=strike,
                    ce_token=pair.ce_token,
                    ce_ltp=ce.get("last_price"),
                    ce_oi=ce_oi,
                    ce_oi_change=self._oi_change(pair.ce_token, ce_oi),
                    ce_volume=ce.get("volume_traded"),
                    ce_iv=None,
                    pe_token=pair.pe_token,
                    pe_ltp=pe.get("last_price"),
                    pe_oi=pe_oi,
                    pe_oi_change=self._oi_change(pair.pe_token, pe_oi),
                    pe_volume=pe.get("volume_traded"),
                    pe_iv=None,
                )
            )

        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else None
        return OptionChain(
            symbol=symbol,
            expiry=universe.expiry.isoformat() if universe.expiry else None,
            atm_strike=atm,
            pcr=pcr,
            rows=rows,
            updated_at=int(time.time()),
        )

    def _oi_change(self, token: int | None, oi: float | None) -> float | None:
        if token is None or oi is None:
            return None
        base = self.state.oi_prev.get(token)
        if base is None:
            # First sighting this session -> baseline, no change yet.
            self.state.oi_prev[token] = oi
            return 0.0
        return round(oi - base, 0)

    def refresh_all(self) -> None:
        # Reset the OI baseline at the IST day rollover so oi_change reflects the
        # current session only (mirrors CandleEngine's daily reset).
        day = _ist_day()
        if day != self._session_day:
            self.state.oi_prev.clear()
            self._session_day = day

        for key in self.universes:
            chain = self.build(key)
            if chain is not None:
                self.state.set_option_chain(key, chain)
