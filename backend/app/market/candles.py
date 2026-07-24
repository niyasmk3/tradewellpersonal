"""Tick -> OHLCV candle aggregation, per instrument, for multiple timeframes.

Kite's WebSocket streams *ticks* (last price + cumulative day volume), not
candles, so we build candles here. One ``CandleEngine`` instance tracks one
instrument across all configured timeframes.

Thread-safety: ``add_tick`` runs on the KiteTicker thread; ``dataframe`` /
``snapshot`` are read from the asyncio API thread. A per-engine lock guards the
candle buffers.
"""
from __future__ import annotations

import threading
from collections import deque

import pandas as pd

from app.models.schemas import Candle

# Timeframe label -> bucket size in seconds. Because NSE's session opens at
# 09:15 IST (UTC+5:30) and all of these divide the 30-minute offset remainder,
# flooring epoch seconds by bucket size aligns candle opens to the conventional
# IST candle grid (09:15, 09:18, 09:20, ...).
TIMEFRAME_SECONDS: dict[str, int] = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}

# Max candles retained per timeframe (plenty for intraday indicators).
_MAX_CANDLES = 600

# Timeframes whose buffers survive the day rollover. A multi-day swing thesis
# lives on the 15m frame: wiping it every 09:15 forced the positional mode to
# re-learn the trend from scratch and pushed its first honest card to ~13:15.
# Session-scoped indicators (VWAP) handle the day boundary downstream — see
# indicators.compute_snapshot, which anchors VWAP to the last session only.
_MULTI_DAY_TFS = frozenset({"15m"})

_IST_OFFSET = 19800  # +5:30 in seconds

# NSE equity/F&O session, seconds since IST midnight: 09:15:00 – 15:30:59.
_SESSION_OPEN_S = 9 * 3600 + 15 * 60
_SESSION_CLOSE_S = 15 * 3600 + 30 * 60 + 59


class CandleEngine:
    def __init__(self, token: int) -> None:
        self.token = token
        self._lock = threading.Lock()
        self._candles: dict[str, deque[dict]] = {tf: deque(maxlen=_MAX_CANDLES) for tf in TIMEFRAME_SECONDS}
        self._prev_cum_vol: float | None = None
        self._session_day: int | None = None

    def _reset(self, session_day: int) -> None:
        # Multi-day frames survive the rollover; everything session-scoped clears.
        for tf, dq in self._candles.items():
            if tf not in _MULTI_DAY_TFS:
                dq.clear()
        self._prev_cum_vol = None
        self._session_day = session_day

    def add_tick(self, ltp: float, cum_volume: float | None, ts: int) -> None:
        """Fold one tick into every timeframe.

        ts        : epoch seconds of the tick (exchange timestamp preferred)
        cum_volume: cumulative traded volume for the day (None for indices)

        Tick sanitation (timestamps come from mixed sources — exchange_timestamp,
        last_trade_time, or wall clock — and can be stale or disordered):
          * only 09:15–15:30 IST ticks build candles (pre-open snapshots and
            post-close stragglers would create spurious buckets);
          * the session resets only on a FORWARD day change (a single stale
            yesterday-stamped tick must not wipe today's series);
          * a tick whose bucket is older than the newest candle is folded into
            that candle's volume but can't append an out-of-order bar.
        """
        if ltp is None:
            return
        ist = ts + _IST_OFFSET
        day = ist // 86400
        sod = ist % 86400  # seconds since IST midnight
        if not (_SESSION_OPEN_S <= sod <= _SESSION_CLOSE_S):
            return
        with self._lock:
            if self._session_day is None or day > self._session_day:
                self._reset(day)
            elif day < self._session_day:
                return  # stale tick from a previous day — ignore

            # Per-tick volume delta from the cumulative day volume.
            vol_delta = 0.0
            if cum_volume is not None:
                if self._prev_cum_vol is not None and cum_volume >= self._prev_cum_vol:
                    vol_delta = cum_volume - self._prev_cum_vol
                self._prev_cum_vol = cum_volume

            for tf, secs in TIMEFRAME_SECONDS.items():
                dq = self._candles[tf]
                bucket = (ts // secs) * secs
                if dq and dq[-1]["ts"] == bucket:
                    c = dq[-1]
                    c["high"] = max(c["high"], ltp)
                    c["low"] = min(c["low"], ltp)
                    c["close"] = ltp
                    c["volume"] += vol_delta
                elif not dq or bucket > dq[-1]["ts"]:
                    dq.append(
                        {"ts": bucket, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": vol_delta}
                    )
                elif vol_delta:
                    dq[-1]["volume"] += vol_delta  # late tick: keep the volume, drop the misplaced bar

    def seed(self, tf: str, rows: list[dict]) -> None:
        """Preload one timeframe with historical candles (restart recovery /
        late start). Rows: ts/open/high/low/close/volume, ascending. Session
        timeframes expect ONE session day; multi-day timeframes (15m) may span
        several. Never replaces a live buffer that already holds at least as
        many candles."""
        if not rows:
            return
        day = (int(rows[-1]["ts"]) + _IST_OFFSET) // 86400
        with self._lock:
            if self._session_day is None or day > self._session_day:
                self._reset(day)
            elif day < self._session_day and tf not in _MULTI_DAY_TFS:
                return  # seeding an older session than live ticks have built
            dq = self._candles.get(tf)
            if dq is None or len(rows) <= len(dq):
                return
            dq.clear()
            for r in rows:
                dq.append({
                    "ts": int(r["ts"]), "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r.get("volume", 0) or 0),
                })

    def dataframe(self, tf: str) -> pd.DataFrame:
        """Return the timeframe's candles as a DataFrame (for indicator math)."""
        with self._lock:
            rows = list(self._candles.get(tf, ()))
        if not rows:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        return pd.DataFrame(rows)

    def snapshot(self, tf: str, limit: int = 240) -> list[Candle]:
        """Return the most recent `limit` candles for the API/chart."""
        with self._lock:
            rows = list(self._candles.get(tf, ()))[-limit:]
        return [Candle(**r) for r in rows]

    def last_price(self) -> float | None:
        with self._lock:
            for dq in self._candles.values():
                if dq:
                    return dq[-1]["close"]
        return None
