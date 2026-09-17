"""Kite sync for the Closing Day study: the price spine + India VIX.

The NIFTY 5-min spine is the Patterns Module's table — this module refreshes it
through patterns' own incremental sync rather than keeping a second copy of the
same 55k bars. India VIX (token 264969) is nobody else's data, so it lands in
this module's own store.

VIX is the study's only volatility input, and it is real: Kite serves 5-min
INDIA VIX candles back ~3 years, verified before this module was written. What
is NOT real is the step from VIX to the ATM implied vol of a specific weekly
contract — that is app/closing/calibration.py's job, fitted against actual
option quotes and published with its error.

Chunking mirrors patterns/data.py (<=60 days per request, small sleep) to stay
under the historical API's 3 req/s cap.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from app.closing import store
from app.patterns import data as patterns_data
from app.closing.calendar import IST

log = logging.getLogger("tradewell.closing")

VIX_TOKEN = 264969
VIX_SYMBOL = "NSE:INDIA VIX"

# The chunked transport and session bounds ARE patterns' — one implementation,
# so a session-bounds change (the CAS lesson) can never land in one fetcher
# and miss the other (review catch: this file carried verbatim copies).
_fetch_chunks = patterns_data._fetch_chunks
_in_session = patterns_data._in_session


def sync_vix(kite, years: int = 3) -> dict:
    """Pull India VIX 5-min bars into the store. Incremental on re-run."""
    now = datetime.now(IST)                 # naive = host-local = wrong off-IST
    start = now - timedelta(days=int(years * 365.25))
    last = store.last_vix_ts()
    if last is not None:
        resume = datetime.fromtimestamp(last, IST).replace(hour=0, minute=0, second=0)
        start = max(start, resume)

    log.info("Closing sync: INDIA VIX from %s to %s", start.date(), now.date())
    rows, seen = [], set()
    for d in _fetch_chunks(kite, VIX_TOKEN, start, now):
        dt = d["date"]
        epoch = int(dt.timestamp())
        if epoch in seen or not _in_session(dt):
            continue
        seen.add(epoch)
        rows.append((epoch, d["open"], d["high"], d["low"], d["close"]))
    written = store.upsert_vix(rows)
    store.set_meta("last_vix_sync", datetime.now(IST).isoformat())
    return {"vix_bars_written": written, "vix_bars_total": store.vix_count()}


def sync(kite, years: int = 3) -> dict:
    """Refresh the shared price spine, then this module's VIX table."""
    spine = patterns_data.sync(kite, years=years)
    vix = sync_vix(kite, years=years)
    summary = {"spine": spine, "vix": vix}
    log.info("Closing sync done: %s", summary)
    return summary
