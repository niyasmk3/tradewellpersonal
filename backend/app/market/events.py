"""Scheduled-event awareness — the calendar the news feed can't see coming.

The news component reacts to headlines AFTER they break; RBI policy, the
Budget, CPI prints and Fed days are known weeks ahead, and the documented
conservative default is smaller size (or no fresh exposure) into them. This
module reads a user-maintained JSON calendar and answers one question: is a
scheduled event close enough to change today's behaviour?

Calendar file: backend/.events.json (per-machine, like the journals):
    [
      {"date": "2026-08-06", "time": "10:00", "label": "RBI policy"},
      {"date": "2026-08-12", "label": "US CPI (evening IST)"}
    ]
`time` optional (IST HH:MM); an event without one is treated as all-day.
Missing/invalid file = no events, everything behaves as before. The file is
data, not code — update it whenever the RBI/Fed calendars publish.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("tradewell.events")

_IST = timezone(timedelta(hours=5, minutes=30))
_PATH = Path(__file__).resolve().parents[2] / ".events.json"
# How far ahead an event starts affecting cards.
_WINDOW_H = 3.0

_cache: dict = {"mtime": None, "events": []}


def _load() -> list[dict]:
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        return []
    if _cache["mtime"] == mtime:
        return _cache["events"]
    try:
        raw = json.loads(_PATH.read_text())
        events = [e for e in raw if isinstance(e, dict) and e.get("date") and e.get("label")]
    except Exception as exc:
        log.warning("events calendar unreadable (%s) — ignoring", exc)
        events = []
    _cache["mtime"], _cache["events"] = mtime, events
    return events


def upcoming(now_ts: int, window_h: float = _WINDOW_H) -> str | None:
    """A caution note when a scheduled event is today and within `window_h`
    hours ahead (or in progress / just past — event volatility does not end
    at the announcement minute).

    Callers size the window to the position's LIFETIME: an intraday (MIS)
    position squared off by ~15:20 cannot reach an evening print, so the
    default 3h window naturally never flags it; a positional card that will be
    held through the event should pass a wider window. All-day (untimed)
    events flag for the whole session — the conservative reading of an entry
    the user chose not to timestamp."""
    now = datetime.fromtimestamp(now_ts, _IST)
    today = now.strftime("%Y-%m-%d")
    for e in _load():
        if e["date"] != today:
            continue
        t = e.get("time")
        if not t:
            return f"{e['label']} today"
        try:
            when = datetime.strptime(f"{e['date']} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=_IST)
        except ValueError:
            return f"{e['label']} today"
        delta_h = (when - now).total_seconds() / 3600
        if -1.0 <= delta_h <= window_h:
            if delta_h > 0:
                return f"{e['label']} at {t} ({delta_h:.1f}h away)"
            return f"{e['label']} at {t} (in progress / just past)"
    return None
