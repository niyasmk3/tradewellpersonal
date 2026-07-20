"""Instrument synchronisation and token resolution.

Downloads the NSE (index) + NFO (F&O) instrument masters from Kite and wires up,
per tracked underlying:
  * the spot index token (price + levels)
  * the near-month FUTURE token (volume / VWAP source for indicators)
  * the nearest option expiry and a strike->(CE,PE) map around ATM

The resolved option universe is what the option-chain builder iterates each poll.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.state import MarketState, UnderlyingMeta

log = logging.getLogger("tradewell.instruments")

# The NSE+NFO masters change once per day (generated pre-market). Caching them
# keyed on IST date makes every restart's feed start near-instant and removes
# ~10MB of re-downloads. Anchored to the backend dir like the other caches.
_CACHE_PATH = Path(__file__).resolve().parents[2] / ".instruments_cache.json"


def chain_key(symbol: str, expiry_key: str) -> str:
    """Compound key for an option chain / universe: e.g. 'NIFTY:nearest'."""
    return f"{symbol.upper()}:{expiry_key}"

# symbol -> spot metadata. Tokens are stable Kite defaults; we still try to
# resolve them from the live dump and fall back to these.
UNDERLYING_CONFIG: dict[str, dict] = {
    "NIFTY": {"spot_symbol": "NIFTY 50", "spot_token": 256265, "step": 50, "nfo_name": "NIFTY"},
    "BANKNIFTY": {"spot_symbol": "NIFTY BANK", "spot_token": 260105, "step": 100, "nfo_name": "BANKNIFTY"},
    "FINNIFTY": {"spot_symbol": "NIFTY FIN SERVICE", "spot_token": 257801, "step": 50, "nfo_name": "FINNIFTY"},
}
VIX_SYMBOL = "INDIA VIX"
VIX_TOKEN = 264969

# We subscribe a strike window wider than what the chain displays, so the chain
# builder can re-centre on the live ATM as price moves intraday (± this many
# extra strikes beyond the display depth).
SUBSCRIBE_MARGIN = 20


@dataclass
class StrikePair:
    strike: float
    ce_token: int | None = None
    pe_token: int | None = None
    ce_symbol: str | None = None
    pe_symbol: str | None = None
    # Per-CONTRACT lot size. NSE applies lot revisions to newly listed series
    # while live contracts keep the old size, so the future's lot size (on
    # UnderlyingMeta) can disagree with the option's for weeks.
    ce_lot_size: int = 0
    pe_lot_size: int = 0


@dataclass
class OptionUniverse:
    symbol: str
    expiry: date | None
    step: int
    strikes: dict[float, StrikePair] = field(default_factory=dict)

    def option_tokens(self) -> list[int]:
        toks: list[int] = []
        for sp in self.strikes.values():
            if sp.ce_token:
                toks.append(sp.ce_token)
            if sp.pe_token:
                toks.append(sp.pe_token)
        return toks


def _ist_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _download(kite, exchange: str) -> list[dict]:
    """One instruments() dump with a single retry — a transient network blip
    during feed start shouldn't take the whole login flow down."""
    try:
        return kite.instruments(exchange)
    except Exception as exc:
        log.warning("instruments(%s) failed (%s) — retrying once", exchange, exc)
        time.sleep(1.0)
        return kite.instruments(exchange)


def _fetch_instruments(kite) -> tuple[list[dict], list[dict]]:
    today = str(_ist_today())
    try:
        if _CACHE_PATH.exists():
            data = json.loads(_CACHE_PATH.read_text())
            if data.get("date") == today and data.get("NSE") and data.get("NFO"):
                log.info("Instruments loaded from disk cache (%s)", today)
                return data["NSE"], data["NFO"]
    except Exception as exc:  # unreadable cache: fall through to a fresh fetch
        log.warning("instruments cache read failed: %s", exc)

    nse = _download(kite, "NSE")
    nfo = _download(kite, "NFO")
    try:
        # default=str turns the date-typed 'expiry' into "YYYY-MM-DD", which
        # _as_date() parses right back on the cached path.
        _CACHE_PATH.write_text(json.dumps({"date": today, "NSE": nse, "NFO": nfo}, default=str))
    except Exception as exc:  # pragma: no cover
        log.warning("instruments cache write failed: %s", exc)
    return nse, nfo


def resolve_universe(kite, settings, state: MarketState):
    """Sync instruments and wire state. Returns (subscription_tokens, universes)."""
    today = _ist_today()
    nse, nfo = _fetch_instruments(kite)
    log.info("Loaded %d NSE + %d NFO instruments", len(nse), len(nfo))

    nse_by_symbol = {i["tradingsymbol"]: i for i in nse}

    subscription: set[int] = set()
    universes: dict[str, OptionUniverse] = {}

    # India VIX
    vix = nse_by_symbol.get(VIX_SYMBOL)
    state.vix_token = vix["instrument_token"] if vix else VIX_TOKEN
    subscription.add(state.vix_token)

    for symbol in settings.underlyings:
        cfg = UNDERLYING_CONFIG.get(symbol)
        if cfg is None:
            log.warning("Unknown underlying %s, skipping", symbol)
            continue

        spot = nse_by_symbol.get(cfg["spot_symbol"])
        spot_token = spot["instrument_token"] if spot else cfg["spot_token"]

        derivs = [i for i in nfo if i.get("name") == cfg["nfo_name"]]

        # --- near-month future ---
        futs = [i for i in derivs if i.get("instrument_type") == "FUT"]
        fut = _nearest(futs, today)
        fut_token = fut["instrument_token"] if fut else None
        fut_symbol = fut["tradingsymbol"] if fut else None

        state.register_underlying(
            UnderlyingMeta(
                symbol=symbol,
                spot_token=spot_token,
                spot_tradingsymbol=cfg["spot_symbol"],
                strike_step=cfg["step"],
                fut_token=fut_token,
                fut_tradingsymbol=fut_symbol,
                lot_size=int(fut.get("lot_size") or 0) if fut else 0,
            )
        )
        subscription.add(spot_token)
        if fut_token:
            subscription.add(fut_token)

        # --- option universes: nearest (weekly) + monthly around ATM ---
        options = [i for i in derivs if i.get("instrument_type") in ("CE", "PE")]
        nearest_expiry = _nearest_expiry(options, today)
        monthly_expiry = _nearest_monthly_expiry(options, today)
        spot_ltp = _spot_ltp(kite, cfg["spot_symbol"]) or (fut.get("last_price") if fut else None)
        # Build wider than the display depth so the chain can re-centre intraday.
        depth = settings.option_chain_depth + SUBSCRIBE_MARGIN

        nearest_u = _build_universe(symbol, cfg["step"], nearest_expiry, options, spot_ltp, depth)
        universes[chain_key(symbol, "nearest")] = nearest_u
        subscription.update(nearest_u.option_tokens())

        # Reuse the nearest universe when the monthly expiry coincides (e.g. BANKNIFTY).
        if monthly_expiry and monthly_expiry != nearest_expiry:
            monthly_u = _build_universe(symbol, cfg["step"], monthly_expiry, options, spot_ltp, depth)
            subscription.update(monthly_u.option_tokens())
        else:
            monthly_u = nearest_u
        universes[chain_key(symbol, "monthly")] = monthly_u

        log.info(
            "%s: fut=%s weekly=%s monthly=%s strikes=%d/%d (ATM~%s)",
            symbol, fut_symbol, nearest_expiry, monthly_expiry,
            len(nearest_u.strikes), len(monthly_u.strikes), spot_ltp,
        )

    state.instruments_loaded = True
    return sorted(subscription), universes


def _nearest(instruments: list[dict], today: date) -> dict | None:
    dated = [(_as_date(i.get("expiry")), i) for i in instruments]
    future = [(d, i) for d, i in dated if d and d >= today]
    if not future:
        return None
    return min(future, key=lambda x: x[0])[1]


def _nearest_expiry(options: list[dict], today: date) -> date | None:
    expiries = sorted({d for d in (_as_date(o.get("expiry")) for o in options) if d and d >= today})
    return expiries[0] if expiries else None


# Positional trades are multi-day swings — a monthly contract expiring within
# this many days can't host one, so the universe rolls to the next month.
_MONTHLY_ROLLOVER_DAYS = 2


def _nearest_monthly_expiry(options: list[dict], today: date) -> date | None:
    """The monthly-series expiry = the last expiry within a calendar month.

    For NIFTY this is the last weekly of the month; for symbols that only trade
    monthly (BANKNIFTY/FINNIFTY) it equals the nearest expiry. On/near the
    monthly expiry day itself the series is rolled to the NEXT month — the
    positional mode must never build multi-day swing cards on a contract dying
    this afternoon (round-2 audit finding).
    """
    by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
    for o in options:
        d = _as_date(o.get("expiry"))
        if d:
            by_month[(d.year, d.month)].append(d)
    monthlies = sorted(max(v) for v in by_month.values())
    cutoff = today + timedelta(days=_MONTHLY_ROLLOVER_DAYS)
    for d in monthlies:
        if d > cutoff:
            return d
    # Fallback: nothing beyond the rollover window listed (shouldn't happen —
    # exchanges list 3 months out) — take whatever is still alive.
    for d in monthlies:
        if d >= today:
            return d
    return None


def _spot_ltp(kite, tradingsymbol: str) -> float | None:
    try:
        key = f"NSE:{tradingsymbol}"
        data = kite.ltp([key])
        return data.get(key, {}).get("last_price")
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("LTP fetch failed for %s: %s", tradingsymbol, exc)
        return None


def _build_universe(
    symbol: str, step: int, expiry: date | None, options: list[dict], spot_ltp: float | None, depth: int
) -> OptionUniverse:
    universe = OptionUniverse(symbol=symbol, expiry=expiry, step=step)
    if expiry is None:
        return universe

    at_expiry = [o for o in options if _as_date(o.get("expiry")) == expiry]

    # Determine which strikes to keep (ATM +/- depth). With no live price (ltp
    # failure at startup), anchor on the median listed strike instead — keeping
    # EVERYTHING would balloon the WS subscription toward Kite's 3000-token cap.
    anchor = spot_ltp
    if not anchor and at_expiry:
        strikes_sorted = sorted(float(o.get("strike") or 0) for o in at_expiry)
        anchor = strikes_sorted[len(strikes_sorted) // 2]
    keep: set[float] | None = None
    if anchor:
        atm = round(anchor / step) * step
        keep = {atm + k * step for k in range(-depth, depth + 1)}

    for o in at_expiry:
        strike = float(o.get("strike") or 0)
        if keep is not None and strike not in keep:
            continue
        pair = universe.strikes.setdefault(strike, StrikePair(strike=strike))
        lot = int(o.get("lot_size") or 0)
        if o["instrument_type"] == "CE":
            pair.ce_token = o["instrument_token"]
            pair.ce_symbol = o["tradingsymbol"]
            pair.ce_lot_size = lot
        else:
            pair.pe_token = o["instrument_token"]
            pair.pe_symbol = o["tradingsymbol"]
            pair.pe_lot_size = lot

    return universe
