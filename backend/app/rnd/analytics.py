"""R&D window analytics — does a signal deliver its % move inside its window?

Reads the paper book directly (honest era, hollow rows excluded — the same
discipline as every clean-book consumer), evaluates each fill against ITS OWN
mode's window, and reports reach rates by bucket with n everywhere.

Two evidence classes, never blended silently:
  * EXACT — rows with the R1 touch ladder (touch_times): first-crossing
    timestamps, the real answer.
  * APPROX (lower bound) — pre-ladder rows: "+5 in window" inferred from
    mfe_at (time of the MAXIMUM). mfe_at inside the window implies the first
    +5 crossing was too (premium must cross 5 on the way to a max >= 5), so
    approx never overcounts — it can only miss touches whose max came later.
    Every aggregate carries exact/approx counts; the UI labels accordingly.

No DB: the book is parsed from .paper_trades.json, keyed on (mtime, size)
like signals/archive.py, because the dashboard will poll this.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

_BOOK_PATH = Path(__file__).resolve().parents[2] / ".paper_trades.json"
_IST_OFFSET = 19800

# Honest-fill era: IMPORTED from the authoritative constant, never copied.
# The review caught the hand-copied literal a week early (1784147400 =
# 16-Jul 02:00 IST, not 23-Jul) — 7 dishonest-era fills in every statistic.
# An import cannot drift. (paper/service.py has no rnd import; no cycle.)
from app.paper.service import HONEST_FILLS_FROM as HONEST_FROM  # noqa: E402

LEVELS = (3.0, 5.0, 10.0, 20.0)
BUCKETS = ("<3", "3-5", "5-10", "10-20", ">20")
REACH_MINUTES = (15, 30, 60, 120, 240)
SCORE_BANDS = (("<78", 0, 78), ("78-81", 78, 82), ("82+", 82, 101))

# A ladder is EXACT evidence only when observation began at entry. A trade
# open across the ladder deploy (or blind through a gap) carries stamps that
# are not first crossings; without this gate such rows are indistinguishable
# from clean ones — the exact-class contamination the R1 review confirmed
# (a censored row read as exact=(False) for +5 when the true touch predated
# its ladder). 120s covers many monitor cycles of startup slack.
_LADDER_FROM_ENTRY_S = 120


def _ladder(t: dict) -> dict:
    """The row's touch ladder IF it qualifies as exact evidence, else {} —
    which routes every consumer down the approx (mfe_at lower-bound) path."""
    touches = t.get("touch_times") or {}
    if not touches:
        return {}
    tf, entered = t.get("touch_from"), t.get("entered_at")
    if not tf or not entered or tf - entered > _LADDER_FROM_ENTRY_S:
        return {}
    return touches

_cache: dict = {"key": None, "rows": None}


def _load_rows() -> list[dict]:
    try:
        st = _BOOK_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    if _cache["key"] == key and _cache["rows"] is not None:
        return _cache["rows"]
    try:
        data = json.loads(_BOOK_PATH.read_text())
    except Exception:
        return _cache["rows"] or []
    # The book is a top-level LIST today; tolerate a {"trades": ...} wrapper
    # (dict-keyed or list) so a future store format change degrades gracefully.
    raw = data.get("trades", data) if isinstance(data, dict) else data
    rows = list(raw.values()) if isinstance(raw, dict) else list(raw or [])
    _cache.update(key=key, rows=rows)
    return rows


def clean_fills(rows: list[dict] | None = None) -> list[dict]:
    rows = _load_rows() if rows is None else rows
    out = []
    for t in rows:
        if not isinstance(t, dict):
            continue
        if (t.get("notes") or "").startswith("hollow:"):
            continue
        if (t.get("entered_at") or 0) < HONEST_FROM:
            continue
        if not t.get("entry_premium"):
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Per-trade window evaluation
# ---------------------------------------------------------------------------

def window_end(t: dict, cfg) -> int | None:
    """Entry + the mode's window; positional additionally capped at the
    configured IST time on the ENTRY day (spec: 240 min capped 14:50).

    None = the window is EMPTY (a positional entered at/after the cutoff):
    the question "did it reach +5 inside the window" is vacuous for such a
    row, and grading it as a guaranteed miss structurally depressed the
    thinnest mode's KPI by ~7pp (review C2). Unmeasurable ≠ failed."""
    entered = int(t.get("entered_at") or 0)
    mode = t.get("mode") or "intraday"
    minutes = {
        "scalp": cfg.rnd_window_scalp_min,
        "intraday": cfg.rnd_window_intraday_min,
        "positional": cfg.rnd_window_positional_min,
    }.get(mode, cfg.rnd_window_intraday_min)
    end = entered + minutes * 60
    if mode == "positional":
        hh, mm = cfg.rnd_positional_cutoff_ist.split(":")
        day_start = ((entered + _IST_OFFSET) // 86400) * 86400 - _IST_OFFSET
        cutoff = day_start + int(hh) * 3600 + int(mm) * 60
        end = min(end, cutoff)
    return end if end > entered else None


def _measurable(fills: list[dict], cfg) -> tuple[list[dict], int, int]:
    """(gradeable rows, n_pending, n_no_window). A row is gradeable only when
    the trade is CLOSED (an open mid-window row is not a miss yet — review
    C6) and its window is non-empty (review C2). Skipped rows are counted,
    never silently dropped — the module's n-everywhere discipline."""
    out, pending, no_window = [], 0, 0
    for t in fills:
        if t.get("status") != "exited":
            pending += 1
            continue
        if window_end(t, cfg) is None:
            no_window += 1
            continue
        out.append(t)
    return out, pending, no_window


def touch_within(t: dict, level: float, end: int) -> tuple[bool | None, bool]:
    """(touched_within, exact). None = unknowable (approx row without the
    needed evidence) — counted as not-reached, surfaced via approx counts."""
    touches = _ladder(t)
    key = f"+{level:g}"
    if touches:
        ts = touches.get(key)
        return (ts is not None and ts <= end), True
    # Approx path (pre-ladder row): lower bound via the max's timestamp.
    entry = t["entry_premium"]
    mfe, mfe_at = t.get("mfe_premium"), t.get("mfe_at")
    if not mfe or not mfe_at:
        return None, False
    mfe_pct = (mfe - entry) / entry * 100.0
    return (mfe_pct >= level and mfe_at <= end), False


def first_touch_minutes(t: dict, level: float) -> tuple[float | None, bool]:
    """Minutes from entry to first touch of +level, (value, exact)."""
    entered = int(t.get("entered_at") or 0)
    touches = _ladder(t)
    ts = touches.get(f"+{level:g}")
    if touches and ts:
        return (ts - entered) / 60.0, True
    entry = t["entry_premium"]
    mfe, mfe_at = t.get("mfe_premium"), t.get("mfe_at")
    if mfe and mfe_at and (mfe - entry) / entry * 100.0 >= level:
        return (mfe_at - entered) / 60.0, False
    return None, touches != {}


def bucket_of(t: dict, end: int) -> str | None:
    """Highest level touched inside the window -> the user's buckets."""
    best = None
    for lv in LEVELS:
        hit, _ = touch_within(t, lv, end)
        if hit:
            best = lv
    if best is None:
        return "<3"
    return {3.0: "3-5", 5.0: "5-10", 10.0: "10-20", 20.0: ">20"}[best]


def heat_before_capture(t: dict, end: int) -> str | None:
    """For a +5-in-window reacher: deepest NEGATIVE level first-touched
    BEFORE the +5 touch (exact rows), or before the max (approx). Answers
    'what drawdown does harvesting the 5% require?'."""
    touches = _ladder(t)
    if touches:
        plus5 = touches.get("+5")
        if plus5 is None:
            return None
        deepest = 0.0
        for lv in LEVELS:
            ts = touches.get(f"-{lv:g}")
            if ts is not None and ts < plus5:
                deepest = max(deepest, lv)
        return f"-{deepest:g}" if deepest else "none"
    entry = t["entry_premium"]
    mae, mae_at, mfe_at = t.get("mae_premium"), t.get("mae_at"), t.get("mfe_at")
    if not (mae and mae_at and mfe_at):
        return None                     # evidence missing ≠ "took no heat" (C4)
    mae_pct = (entry - mae) / entry * 100.0
    if mae_at >= mfe_at:
        # Global min came AFTER the peak: any pre-peak dip is bounded by it,
        # so "none" is provable only when even the global min is sub-level.
        return "none" if mae_pct < LEVELS[0] else None
    deepest = 0.0
    for lv in LEVELS:
        if mae_pct >= lv:
            deepest = lv
    return f"-{deepest:g}" if deepest else "none"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _kpi(fills: list[dict], cfg) -> dict:
    fills, n_pending, n_no_window = _measurable(fills, cfg)
    n = len(fills)
    exact = [t for t in fills if _ladder(t)]
    reach5 = []
    # Never blended (review C3): exact values are first crossings; approx
    # values are time-of-PEAK — an upper bound on time-to-5, published under
    # its own honest name.
    mins_to5_exact: list[float] = []
    mins_to_peak_approx: list[float] = []
    buckets = {b: 0 for b in BUCKETS}
    curve = {m: {f"p{lv:g}": 0 for lv in LEVELS} for m in REACH_MINUTES}
    for t in fills:
        end = window_end(t, cfg)
        hit, _ = touch_within(t, 5.0, end)
        reach5.append(bool(hit))
        b = bucket_of(t, end)
        if b:
            buckets[b] += 1
        m5, m5_exact = first_touch_minutes(t, 5.0)
        if hit and m5 is not None:
            (mins_to5_exact if m5_exact else mins_to_peak_approx).append(m5)
        entered = int(t.get("entered_at") or 0)
        # The curve is deliberately UNcapped by the mode window — it shows the
        # raw reach-by-time shape; the window verdict lives in p5_in_window.
        for m in REACH_MINUTES:
            for lv in LEVELS:
                h, _ = touch_within(t, lv, entered + m * 60)
                if h:
                    curve[m][f"p{lv:g}"] += 1
    pct = lambda k: round(100.0 * k / n, 1) if n else None
    med = lambda xs: round(statistics.median(xs), 1) if xs else None
    return {
        "n": n,
        "n_exact": len(exact),
        "n_approx": n - len(exact),
        "n_pending": n_pending,
        "n_no_window": n_no_window,
        "p5_in_window": pct(sum(reach5)),
        "median_min_to_5_exact": med(mins_to5_exact),
        "n_exact_times": len(mins_to5_exact),
        "median_min_to_peak_approx": med(mins_to_peak_approx),
        "buckets": {b: {"n": c, "pct": pct(c)} for b, c in buckets.items()},
        "reach_curve": [
            {"minutes": m, **{k: pct(v) for k, v in curve[m].items()}}
            for m in REACH_MINUTES
        ],
    }


def _cut(fills: list[dict], cfg, keyfn) -> list[dict]:
    fills, _, _ = _measurable(fills, cfg)
    groups: dict = {}
    for t in fills:
        k = keyfn(t)
        if k is None:
            continue
        groups.setdefault(k, []).append(t)
    out = []
    for k, g in sorted(groups.items(), key=lambda kv: str(kv[0])):
        hits = sum(1 for t in g if touch_within(t, 5.0, window_end(t, cfg))[0])
        out.append({
            "key": str(k), "n": len(g),
            "p5_in_window": round(100.0 * hits / len(g), 1),
            "sufficient": len(g) >= cfg.rnd_min_sample,
        })
    return out


def _score_band(t: dict):
    s = t.get("entry_score")
    if s is None:
        return None
    for name, lo, hi in SCORE_BANDS:
        if lo <= s < hi:
            return name
    return None


def _hour(t: dict):
    ts = t.get("entered_at")
    return f"{int((ts + _IST_OFFSET) // 3600 % 24):02d}h" if ts else None


def _dow(t: dict):
    ts = t.get("entered_at")
    if not ts:
        return None
    # Epoch day 0 (1-Jan-1970) is a Thursday -> index 3 in a Mon-first week.
    return ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[
        (int((ts + _IST_OFFSET) // 86400) % 7 + 3) % 7]


def summary(cfg) -> dict:
    fills = clean_fills()
    modes: dict = {}
    for mode in ("scalp", "intraday", "positional"):
        modes[mode] = _kpi([t for t in fills if t.get("mode") == mode], cfg)
    heat: dict = {}
    gradeable, _, _ = _measurable(fills, cfg)
    for t in gradeable:
        end = window_end(t, cfg)
        hit, _ = touch_within(t, 5.0, end)
        if hit:
            h = heat_before_capture(t, end)
            if h:
                cls = "exact" if _ladder(t) else "approx"
                heat.setdefault(cls, {}).setdefault(h, 0)
                heat[cls][h] += 1
    return {
        "generated_at": int(time.time()),
        "windows_min": {
            "scalp": cfg.rnd_window_scalp_min,
            "intraday": cfg.rnd_window_intraday_min,
            "positional": cfg.rnd_window_positional_min,
        },
        "positional_cutoff_ist": cfg.rnd_positional_cutoff_ist,
        "min_sample": cfg.rnd_min_sample,
        "modes": modes,
        "cuts": {
            "score_band": _cut(fills, cfg, _score_band),
            "tape_state": _cut(fills, cfg, lambda t: t.get("tape_state")),
            "golden": _cut(fills, cfg, lambda t: str(bool(t.get("golden")))),
            "entry_hour": _cut(fills, cfg, _hour),
            "day_of_week": _cut(fills, cfg, _dow),
            "direction": _cut(fills, cfg, lambda t: t.get("direction")),
        },
        "heat_before_capture": heat,
        "notes": [
            "Approx rows (pre-ladder) are a LOWER BOUND: a +5 touch whose max "
            "came after the window is invisible to them.",
            "Reach rates are gross premium moves — signal quality, not net P&L. "
            "The paper book's own summary stays the net-of-charges truth.",
            "Cuts pool all modes; each trade is judged against its own mode's "
            "window. Cells under the minimum sample are marked insufficient.",
        ],
    }


def ledger(cfg, limit: int = 200) -> tuple[list[dict], int]:
    """(rows, total clean fills) — total is pre-slice, so the UI's 'showing
    X of Y' means the book, not the fetch cap (review C9)."""
    fills = clean_fills()
    out = []
    for t in sorted(fills, key=lambda x: x.get("entered_at") or 0,
                    reverse=True)[:limit]:
        end = window_end(t, cfg)
        entry = t["entry_premium"]
        mfe, mae = t.get("mfe_premium"), t.get("mae_premium")
        pending = t.get("status") != "exited"
        gradeable = (not pending) and end is not None
        hit5, exact = (touch_within(t, 5.0, end) if gradeable else (None, False))
        m5, _ = first_touch_minutes(t, 5.0)
        out.append({
            "id": t.get("id"), "mode": t.get("mode"), "contract": t.get("contract"),
            "entered_at": t.get("entered_at"), "entry": entry,
            "score": t.get("entry_score"), "tape": t.get("tape_state"),
            "golden": bool(t.get("golden")),
            "exit_reason": t.get("auto_close_reason") or t.get("exit_reason"),
            "realized_pnl": t.get("realized_pnl"),
            "mfe_pct": round((mfe - entry) / entry * 100, 1) if mfe else None,
            "mae_pct": round((mae - entry) / entry * 100, 1) if mae else None,
            "bucket": bucket_of(t, end) if gradeable else None,
            # None = not gradeable (open mid-window, or a positional entered
            # after the cutoff — no window exists). Never a fake False.
            "p5_in_window": bool(hit5) if gradeable else None,
            "not_gradeable_reason": (None if gradeable else
                                     ("window pending" if pending
                                      else "entered after positional cutoff — no window")),
            "min_to_5": round(m5, 1) if m5 is not None else None,
            "exact": bool(_ladder(t)),
            "touches": t.get("touch_times") or {},   # raw ladder shown even when censored
        })
    return out, len(fills)
