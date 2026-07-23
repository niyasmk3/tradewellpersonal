"""Rolling history of the directional scores — the trend behind the number.

WHY: the dashboard shows "Best-direction score 66/100" as a snapshot, but a 66
that climbed from 40 over the last hour and a 66 decaying from 85 are opposite
situations. The engine already computes every component on every evaluation
(~5s); it just threw the series away. This keeps a bounded in-memory tail so
the UI can show a sparkline when a component row is hovered.

Deliberately NOT persisted: scores are derived entirely from candles and chain
state, so after a restart the history rebuilds itself at one point per
evaluation. Persisting it would only replay stale market context. Same-day
only — on the first record of a new IST day the buffer clears, because a trend
line across a market close is fiction.

Never raises into the caller: this is recorded from the live evaluation loop,
and a diagnostics feature must not be able to stop signal generation.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

log = logging.getLogger("tradewell.signals")

# ~8.3 hours at one evaluation per 5s — comfortably a full session.
_MAXLEN = 6000
_IST_OFFSET = 19800


def _ist_day(ts: int) -> int:
    return (ts + _IST_OFFSET) // 86400


class ScoreHistoryStore:
    def __init__(self, maxlen: int = _MAXLEN) -> None:
        self._lock = threading.Lock()
        self._maxlen = maxlen
        self._series: dict[str, deque] = {}

    @staticmethod
    def _key(symbol: str, mode: str) -> str:
        return f"{symbol.upper()}:{mode}"

    def record(
        self,
        symbol: str,
        mode: str,
        ts: int,
        bull: float,
        bear: float,
        direction: str | None,
        components: dict[str, float] | None,
    ) -> None:
        """Append one evaluation. Swallows everything — see module doc.

        Inputs are coerced UP FRONT: a malformed value must become a skipped
        record here, not a poisoned point that detonates series() later.
        """
        try:
            ts, bull, bear = int(ts), float(bull), float(bear)
            with self._lock:
                dq = self._series.setdefault(
                    self._key(symbol, mode), deque(maxlen=self._maxlen)
                )
                if dq and _ist_day(dq[-1]["ts"]) != _ist_day(ts):
                    dq.clear()
                # The eval loop can run several times inside one epoch second
                # (startup catch-up); one point per second is plenty.
                if dq and dq[-1]["ts"] == ts:
                    dq[-1] = {"ts": ts, "bull": bull, "bear": bear,
                              "direction": direction, "components": components or {}}
                    return
                dq.append({"ts": ts, "bull": bull, "bear": bear,
                           "direction": direction, "components": components or {}})
        except Exception:  # pragma: no cover - defensive by contract
            log.debug("score history record failed", exc_info=True)

    def series(
        self, symbol: str, mode: str, minutes: int = 120, max_points: int = 360
    ) -> dict[str, Any]:
        """The tail of the series, oldest first, decimated to ≤ max_points.

        Decimation keeps every nth point but ALWAYS the newest one — the
        current value is the one the sparkline's endpoint must agree with.
        """
        with self._lock:
            dq = self._series.get(self._key(symbol, mode))
            pts = list(dq) if dq else []
        if pts and minutes > 0:
            cutoff = pts[-1]["ts"] - minutes * 60
            pts = [p for p in pts if p["ts"] >= cutoff]
        if len(pts) > max_points > 0:
            step = -(-len(pts) // max_points)          # ceil division
            kept = pts[::step]
            if kept[-1] is not pts[-1]:
                kept.append(pts[-1])
            pts = kept
        return {"points": pts, "count": len(pts)}


score_history = ScoreHistoryStore()
