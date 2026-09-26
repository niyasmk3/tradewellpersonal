"""Real-quote legs for the Closing Day ledger — the model, audited nightly.

Kite deletes an option contract shortly after expiry, which is why the
ledger's IN/OUT premiums are modelled. But every contract is ALIVE from
listing to expiry and serves real 5-minute bars for that whole window — so a
recent night's REAL fills can sit beside the modelled ones. The 02-Sep
spot-check that prompted this column: modelled IN 119.11 vs real 131.85
(−9.7%), OUT 200.28 vs 205.75 (−2.7%) — both inside the validation block's
published error band, and the night's modelled +67% was +56% on the tape.

Execution convention matches study.py exactly: the entry leg is the CLOSE of
the entry day's 15:00 bar (the 15:05 fill), the exit leg is the OPEN of the
exit day's 09:50 bar. Charges are the same app/paper/charges schedule the
study pays, so real_net_rs and net_rs differ only by the premium legs.

The cache is append-only truth. A night's leg, once stored, is never
refetched — the tape does not change — so real quotes survive long after
Kite deletes the contract. Fetching happens inside analyze when a Kite
session is present; without one, annotation still serves whatever the cache
already holds. A night whose contract is already gone simply stays blank:
the column is honest about being forward-only.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from app.closing.calendar import IST
from app.paper.charges import charges

log = logging.getLogger("tradewell.closing")

CACHE_PATH = Path(__file__).resolve().parents[2] / ".closing_realquotes.json"

ENTRY_BAR = (15, 0)      # its CLOSE is the 15:05 fill — study.py's entry
EXIT_BAR = (9, 50)       # its OPEN is the 09:50 print — study.py's exit
MAX_FETCH_PER_RUN = 10   # newest nights first; the rest complete on later runs
_FETCH_SLEEP_S = 0.35    # under Kite's 3 req/s historical cap

# Every stamped field, in one place, so annotate() can clear them all and a
# consumer (xlsx export, UI) can discover the set without grepping.
FIELDS = ("real_in", "real_out", "real_net_pct", "real_net_rs", "real_delta_rs")


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log.warning("Real-quote cache unreadable at %s — starting empty", path)
        return {}


def _save(cache: dict, path: Path) -> None:
    # tmp + rename, same as store.save_results: the morning auto-grade writes
    # this while the tab's poll may be reading it.
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=1))
        tmp.replace(path)
    except OSError:
        log.warning("Real-quote cache unwritable at %s", path)


def _nfo_instruments(kite) -> list:
    """The NFO master, via the daily disk cache. Isolated so tests stub it."""
    from app.kite.instruments import _fetch_instruments

    return _fetch_instruments(kite)[1]


def _token_for(nfo: list, expiry: date, strike: float, kind: str) -> Optional[int]:
    from app.kite.instruments import _as_date

    for i in nfo:
        if (i.get("name") == "NIFTY" and i.get("instrument_type") == kind
                and _as_date(i.get("expiry")) == expiry
                and float(i.get("strike") or 0) == float(strike)):
            return int(i["instrument_token"])
    return None


def _legs_from_bars(bars: list, entry_date: str, exit_date: str) -> dict:
    """Pick the two real prints out of a fetched bar window. Missing bars stay
    absent — an exit bar that has not printed yet completes on a later run."""
    out: dict = {}
    for b in bars:
        dt = b["date"]  # tz-aware IST from Kite
        hm = (dt.hour, dt.minute)
        d = dt.date().isoformat()
        if d == entry_date and hm == ENTRY_BAR:
            out["real_in"] = float(b["close"])
            out["real_in_at"] = dt.isoformat()
        elif d == exit_date and hm == EXIT_BAR:
            out["real_out"] = float(b["open"])
            out["real_out_at"] = dt.isoformat()
    return out


def backfill(kite, trades: list, path: Path = CACHE_PATH,
             max_fetch: int = MAX_FETCH_PER_RUN) -> dict:
    """Fetch real premium legs for ledger nights whose contract still exists.

    Newest nights first — those are the fetchable ones; anything past its
    expiry is skipped without an API call because Kite has deleted it. One
    historical_data request covers both legs of a night. Returns a small
    summary for the log."""
    if kite is None or not trades:
        return {"fetched": 0, "skipped_expired": 0}
    cache = _load(path)
    nfo = None  # resolved lazily — most runs need zero fetches
    # IST, never host-local (house review catch): on a non-IST box a local
    # date would mis-classify tonight's contract as expired or alive.
    today = datetime.now(IST).date()
    fetched = skipped_expired = 0
    dirty = False
    for t in reversed(trades):
        if fetched >= max_fetch:
            break
        night = t["date"]
        row = cache.get(night) or {}
        if "real_in" in row and "real_out" in row:
            continue
        expiry = date.fromisoformat(t["expiry"])
        if expiry < today:
            skipped_expired += 1
            continue
        try:
            if nfo is None:
                nfo = _nfo_instruments(kite)
            token = _token_for(nfo, expiry, t["strike"], t["direction"])
            if token is None:
                log.warning("Real-quote: no contract for %s %s %s",
                            t["expiry"], t["strike"], t["direction"])
                continue
            frm = datetime.fromisoformat(night) + timedelta(hours=14, minutes=55)
            to = datetime.fromisoformat(t["exit_date"]) + timedelta(hours=10)
            bars = kite.historical_data(token, frm, to, "5minute")
            fetched += 1
            time.sleep(_FETCH_SLEEP_S)
        except Exception as exc:
            log.warning("Real-quote fetch failed for %s: %s", night, exc)
            continue
        legs = _legs_from_bars(bars, night, t["exit_date"])
        if legs:
            legs.update({"symbol_strike": t["strike"], "direction": t["direction"],
                         "expiry": t["expiry"]})
            row.update(legs)
            cache[night] = row
            dirty = True
    if dirty:
        _save(cache, path)
    if fetched:
        log.info("Real-quote backfill: %d night(s) fetched, %d expired-skipped",
                 fetched, skipped_expired)
    return {"fetched": fetched, "skipped_expired": skipped_expired}


def annotate(trades: list, path: Path = CACHE_PATH) -> None:
    """Stamp real_* on each ledger row from the cache; unknown stays None.

    real_net_rs pays the study's own charge schedule on the real fills, so the
    only difference between it and net_rs is the premium legs themselves.
    real_delta_rs = real − modelled: positive means the model UNDERSTATED the
    night."""
    cache = _load(path)
    for t in trades:
        for k in FIELDS:
            t[k] = None
        row = cache.get(t["date"])
        if not row:
            continue
        real_in, real_out = row.get("real_in"), row.get("real_out")
        if real_in is not None:
            t["real_in"] = round(real_in, 2)
        if real_out is not None:
            t["real_out"] = round(real_out, 2)
        if real_in is None or real_out is None or real_in <= 0:
            continue
        qty = t["qty"]
        gross = (real_out - real_in) * qty
        net = gross - charges(real_in, real_out, qty, 2)
        t["real_net_pct"] = round(net / (real_in * qty) * 100, 2)
        t["real_net_rs"] = round(net, 2)
        t["real_delta_rs"] = round(net - t["net_rs"], 2)
