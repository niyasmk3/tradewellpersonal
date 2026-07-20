"""Historical candle fetch via Kite (requires the Historical Data add-on).

Fetches the near-month future's own intraday history. Index futures are listed
~3 months before expiry, so the active near-month contract already covers the
backtest lookback (<=90 days) — no continuous-contract stitching is needed
(Kite rejects ``continuous`` for intraday intervals anyway). Splits the range
into <=60-day chunks (Kite's per-request limit for intraday intervals) and
concatenates. Any failure (no add-on, auth) propagates for the route to surface.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from app.backtest.engine import _day as _day_bucket  # single source of truth for IST day-bucketing

log = logging.getLogger("tradewell.backtest")

_TF_TO_KITE = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute"}
_CHUNK_DAYS = 60
_VIX_CHUNK_DAYS = 1800   # "day" interval allows very large windows


def fetch_futures(kite, token: int, from_dt: datetime, to_dt: datetime, timeframe: str) -> pd.DataFrame:
    interval = _TF_TO_KITE.get(timeframe, "3minute")
    rows: list[dict] = []
    seen: set[int] = set()

    cursor = from_dt
    while cursor < to_dt:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), to_dt)
        candles = kite.historical_data(token, cursor, chunk_end, interval)
        for d in candles:
            epoch = int(d["date"].timestamp())  # tz-aware IST datetime
            if epoch in seen:
                continue
            seen.add(epoch)
            rows.append({
                "ts": epoch, "open": d["open"], "high": d["high"],
                "low": d["low"], "close": d["close"], "volume": d.get("volume", 0) or 0,
            })
        # Start the next chunk AT the boundary (not +1min): the boundary candle is
        # re-fetched but deduped by `seen`, so no candle is ever skipped.
        cursor = chunk_end

    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def fetch_vix_daily(kite, token: int, from_dt: datetime, to_dt: datetime) -> dict[int, float]:
    """India VIX daily closes, keyed by IST day-bucket (for regime/volatility context).

    Best-effort: the caller treats any failure as "no VIX" and proceeds. Daily
    granularity is sufficient — VIX status is bucketed (Calm/Stable/Elevated/High)
    and the engine uses the *prior* day's close, so intraday alignment is moot.
    """
    out: dict[int, float] = {}
    cursor = from_dt
    while cursor < to_dt:
        chunk_end = min(cursor + timedelta(days=_VIX_CHUNK_DAYS), to_dt)
        for d in kite.historical_data(token, cursor, chunk_end, "day"):
            close = d.get("close")
            if close is not None:
                out[_day_bucket(int(d["date"].timestamp()))] = float(close)
        cursor = chunk_end
    return out
