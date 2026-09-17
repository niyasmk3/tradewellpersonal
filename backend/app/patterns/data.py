"""Kite historical sync for the Patterns Module.

NIFTY 50 index (token 256265) 5-minute OHLC is the price spine. NSE indices
report no traded volume, so NIFTYBEES (the most liquid NIFTY 50 ETF) supplies
a 5-minute volume *proxy* aligned by bar timestamp — good for relative
time-of-day volume shape, not absolute contract counts.

Chunked <=60 days per request (Kite's intraday limit is 100; 60 matches the
existing backtest fetcher) with a small sleep between chunks to stay under the
historical API's 3 req/s cap. Incremental: re-sync restarts from the last
stored bar's day, INSERT OR REPLACE dedupes the overlap.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from app.patterns import store
from app.market.calendar import IST

log = logging.getLogger("tradewell.patterns")

NIFTY_TOKEN = 256265
VOLUME_PROXY_SYMBOL = "NSE:NIFTYBEES"
INTERVAL = "5minute"
_CHUNK_DAYS = 60
_CHUNK_SLEEP_S = 0.4

# Regular NSE session only. Bars outside 09:15-15:25 (last 5-min bar opens
# 15:25) are Muhurat/special sessions that would pollute day-of-week stats.
_SESSION_OPEN = (9, 15)
_SESSION_LAST_BAR = (15, 25)


def _in_session(dt: datetime) -> bool:
    if dt.weekday() > 4:  # Muhurat sessions can fall on Saturdays
        return False
    hm = (dt.hour, dt.minute)
    return _SESSION_OPEN <= hm <= _SESSION_LAST_BAR


def _fetch_chunks(kite, token: int, from_dt: datetime, to_dt: datetime):
    """Yield raw Kite candle dicts across chunked requests."""
    cursor = from_dt
    while cursor < to_dt:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), to_dt)
        for d in kite.historical_data(token, cursor, chunk_end, INTERVAL):
            yield d
        cursor = chunk_end
        time.sleep(_CHUNK_SLEEP_S)


def resolve_proxy_token(kite) -> int:
    data = kite.ltp(VOLUME_PROXY_SYMBOL)
    return int(data[VOLUME_PROXY_SYMBOL]["instrument_token"])


def sync(kite, years: int = 3) -> dict:
    """Pull index OHLC + proxy volume into the store. Returns a summary dict."""
    # IST, never host-local: Kite parses a naive from/to as IST, so a naive
    # datetime.now() on a UTC+4 host ended every sync 90 minutes early (17-Sep).
    now = datetime.now(IST)
    start = now - timedelta(days=int(years * 365.25))
    last = store.last_ts()
    if last is not None:
        # Restart from the beginning of the last stored day so a partial final
        # day is refetched whole; overlap is deduped by the ts primary key.
        resume = datetime.fromtimestamp(last, IST).replace(hour=0, minute=0, second=0)
        start = max(start, resume)

    log.info("Patterns sync: index %s from %s to %s", NIFTY_TOKEN, start.date(), now.date())
    rows = []
    seen = set()
    for d in _fetch_chunks(kite, NIFTY_TOKEN, start, now):
        dt = d["date"]  # tz-aware IST
        epoch = int(dt.timestamp())
        if epoch in seen or not _in_session(dt):
            continue
        seen.add(epoch)
        rows.append((epoch, d["open"], d["high"], d["low"], d["close"]))
    written = store.upsert_candles(rows)

    # Volume proxy pass. Best-effort: OHLC analysis stands on its own if the
    # ETF fetch fails, so a proxy error must not fail the sync.
    proxy_updated = 0
    proxy_error = None
    try:
        proxy_token = resolve_proxy_token(kite)
        pairs = []
        for d in _fetch_chunks(kite, proxy_token, start, now):
            dt = d["date"]
            if not _in_session(dt):
                continue
            pairs.append((int(dt.timestamp()), float(d.get("volume", 0) or 0)))
        proxy_updated = store.update_vol_proxy(pairs)
    except Exception as exc:
        proxy_error = str(exc)
        log.warning("Volume-proxy sync failed (analysis continues without it): %s", exc)

    store.set_meta("last_sync", datetime.now(IST).isoformat())
    summary = {
        "index_bars_written": written,
        "proxy_bars_matched": proxy_updated,
        "total_bars": store.candle_count(),
        "proxy_error": proxy_error,
    }
    log.info("Patterns sync done: %s", summary)
    return summary
