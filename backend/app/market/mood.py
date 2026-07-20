"""Market Mood Index (Tickertape) — read-only market-wide Fear/Greed gauge.

A best-effort context reading, NOT a signal input. Fetched from Tickertape's
undocumented public JSON endpoint, TTL-cached, and gracefully degrading: any
failure returns the last-known value (or None) so the UI never breaks and the
signal engine is entirely unaffected.

The MMI (0-100) is a contrarian sentiment gauge; its components (India VIX,
momentum, FII activity, skew) partly overlap inputs the engine already computes,
which is why it's surfaced as context rather than folded into the 0-100 score.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Optional

from pydantic import BaseModel

log = logging.getLogger("tradewell.mood")

_URL = "https://api.tickertape.in/mmi/now"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
_TTL = 600.0          # MMI updates slowly (hours); 10-min cache is plenty
_TIMEOUT = 8.0

_lock = threading.Lock()
_cache: dict = {"mood": None, "ts": 0.0}


class MarketMood(BaseModel):
    value: float                 # 0-100 Market Mood Index
    zone: str                    # Extreme Fear | Fear | Greed | Extreme Greed
    nifty: Optional[float] = None
    date: Optional[str] = None   # source timestamp (ISO)
    updated_at: int              # our fetch epoch
    source: str = "Tickertape MMI"


def _zone(v: float) -> str:
    if v < 30:
        return "Extreme Fear"
    if v < 50:
        return "Fear"
    if v < 70:
        return "Greed"
    return "Extreme Greed"


def _fetch() -> Optional[MarketMood]:
    try:
        req = urllib.request.Request(_URL, headers={"User-Agent": _UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
        data = payload.get("data") or {}
        raw = data.get("indicator")
        if raw is None:
            return None
        val = round(float(raw), 2)
        nifty = data.get("nifty")
        return MarketMood(
            value=val, zone=_zone(val),
            nifty=round(float(nifty), 2) if nifty is not None else None,
            date=data.get("date"), updated_at=int(time.time()),
        )
    except Exception as exc:  # network / parse / shape change — never fatal
        log.warning("MMI fetch failed: %s", exc)
        return None


def get_market_mood(force: bool = False) -> Optional[MarketMood]:
    """Cached MMI. Returns the last-known value on fetch failure (graceful)."""
    with _lock:
        cached = _cache["mood"]
        fresh = cached is not None and (time.time() - _cache["ts"]) < _TTL
    if fresh and not force:
        return cached

    mood = _fetch()
    if mood is not None:
        with _lock:
            _cache["mood"] = mood
            _cache["ts"] = time.time()
        return mood
    return cached  # stale-but-present beats nothing; None if we never succeeded
