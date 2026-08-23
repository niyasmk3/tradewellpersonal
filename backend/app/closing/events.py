"""Scheduled US macro events in the hold window — a SHADOW flag on the card.

REGISTERED 2026-08-23. The trade holds from the 15:05 IST fill on D to the
09:50 IST exit on the next session. A US release dated D lands inside that
window: FOMC decisions at 14:00 ET (23:30 / 00:30 IST) and the 08:30 ET
releases (18:00 / 19:00 IST) — CPI and the Employment Situation (NFP).
Events dated D+1 land after the exit and do not count; RBI (10:00 IST) and
the Union Budget (11:00 IST) fall after the exit too and are not carried.

    event_tonight = any of {FOMC, CPI, NFP} dated D
    unknown (year not covered by the calendar) -> None, never False

THE EVIDENCE (23-Aug, 3y modelled ledger, 87 event nights / 23 CLEAN). The
magnitude claim held in BOTH windows: CLEAN event nights moved 152 vs 85 pts
in-sample and 191 vs 125 in the holdout (all nights 105/83 and 149/115), and
mean P&L was higher in both (+53% vs +7%, +71% vs +26%; bootstrap P 0.87).
The hit-rate claim did NOT: in-sample the index went the trade's way on 44%
of event nights vs 60%, win rate 38% vs 46% (P 0.47); the in-sample mean is
two NFP nights (04-Apr-25 +790%, 02-Aug-24 +224%), median -23%. India VIX did
not price the event in-sample (premium Rs104 vs Rs109). FLAGGED x event still
lost. So this is a PAYOUT-SHAPE flag — the same coin pays more when it lands
— not a filter that separates winners from losers. Shadow only: chip, logged
value, ledger stamp, backfill table; it never touches the checks, the
verdict, or the tiers. n=23 CLEAN nights; graded by live nights.

DATA. data/us_events.json: FOMC decision days from the Fed's calendar page,
CPI and Employment Situation release days from the BLS schedules (actual
dates, including the 2025 shutdown shifts), 2023-2026. Extend the file when
the next year's schedules are published; a date outside coverage_years is
UNKNOWN.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

REGISTERED_ON = "2026-08-23"
KINDS = ("FOMC", "CPI", "NFP")
DATA_PATH = Path(__file__).resolve().parent / "data" / "us_events.json"
DEFINITION = ("event_tonight = a scheduled US release dated the entry day (FOMC "
              "decision 14:00 ET; CPI / Employment Situation 08:30 ET), i.e. "
              "landing between the 15:05 fill and the 09:50 exit. Unknown when "
              "the calendar does not cover the year. Shadow only — never a gate.")

_cache: Optional[dict] = None


def load(path: Path = DATA_PATH) -> dict:
    global _cache
    if _cache is not None and path == DATA_PATH:
        return _cache
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = {"coverage_years": [], "events": {}}
    data = {"years": set(raw.get("coverage_years") or []),
            "events": {d: list(k) for d, k in (raw.get("events") or {}).items()}}
    if path == DATA_PATH:
        _cache = data
    return data


def covers(d: date, data: Optional[dict] = None) -> bool:
    return d.year in (data or load())["years"]


def tonight(d: date, data: Optional[dict] = None) -> Optional[list]:
    """Events landing in the hold window that starts on `d`, or None when
    the calendar does not cover `d`'s year (unknown never passes)."""
    data = data or load()
    if not covers(d, data):
        return None
    return [k for k in data["events"].get(d.isoformat(), []) if k in KINDS]


def annotate(trades: list, data: Optional[dict] = None) -> None:
    data = data or load()
    for t in trades:
        ev = tonight(date.fromisoformat(t["date"]), data)
        t["events"] = ev
        t["f_event"] = (bool(ev)) if ev is not None else None


def summary(trades: list, split: date) -> dict:
    """CLEAN x event / no event (+ FLAGGED x event as the no-rescue control),
    per window, with the mean |index move| — the magnitude is the claim."""
    from app.closing.attribution import _compact
    import statistics

    def cell(rows):
        c = _compact(rows)
        if rows:
            c["abs_move_mean"] = round(statistics.mean(abs(t["signed_move_pts"]) for t in rows), 1)
            c["idx_continued_pct"] = round(sum(1 for t in rows if t["signed_move_pts"] > 0) / len(rows) * 100, 1)
        return c

    def cells(sub):
        clean = [t for t in sub if t.get("card_verdict") == "CLEAN"]
        flagged = [t for t in sub if t.get("card_verdict") == "FLAGGED"]
        return {"clean_event": cell([t for t in clean if t.get("f_event") is True]),
                "clean_no_event": cell([t for t in clean if t.get("f_event") is False]),
                "flagged_event": cell([t for t in flagged if t.get("f_event") is True])}

    windows = {}
    for key, sub in (
            ("in_sample_2y", [t for t in trades if date.fromisoformat(t["date"]) < split]),
            ("holdout_1y", [t for t in trades if date.fromisoformat(t["date"]) >= split]),
            ("pooled_3y", trades)):
        windows[key] = cells(sub)
    by_kind = {k: _compact([t for t in trades if t.get("card_verdict") == "CLEAN"
                            and t.get("events") and k in t["events"]]) for k in KINDS}
    return {
        "registered_on": REGISTERED_ON,
        "definition": DEFINITION,
        "kinds": list(KINDS),
        "windows": windows,
        "by_kind_clean_3y": by_kind,
        "coverage": {"stamped": sum(1 for t in trades if t.get("f_event") is not None),
                     "ledger": len(trades)},
        "note": ("Shadow flag, registered 23-Aug: a scheduled US release (FOMC / "
                 "CPI / NFP) landing between the fill and the exit. In the "
                 "backtest it raised the size of the overnight move in both "
                 "windows but NOT the hit rate — the same coin pays more when it "
                 "lands. A payout-shape signal, never a filter; never touches the "
                 "verdict or the tiers. Graded by live nights."),
    }
