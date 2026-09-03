"""Tonight at 15:00 — the live pre-trade read for the Closing Day rule.

The backtest's five entry-time risk checks, evaluated on TODAY's tape so the
user can look at one card at 15:00 and decide. Every check is knowable before
the 15:05 fill, and every one already exists elsewhere in the repo as a tested
or registered hypothesis — this module ADDS NO NEW RULES, it only reads the
registered ones live:

    last-hour agreement   signals.lasthr_confirm      (Round 3)
    vol expansion         overnight/filters.py        (Round 4, verified)
    mid-range exclusion   overnight/filters.py        (Round 4, near-survivor)
    holiday bridge        closing/attribution.py      (registered 19-Aug)
    month-end entry       closing/attribution.py      (registered 19-Aug)

HONESTY BOX. The 0-red-flags cell made money in both backtest windows
(in-sample +13.4%/trade n=132, holdout +29.2% n=66) and every flagged pool
lost in both — but the five checks were composed AFTER seeing those windows,
so the composite itself is a hypothesis, graded only by live nights. Clean
nights are rare (~5-6/month), and a clean card still lost worse than -50% on
~21% of historical nights: the checks predict the theta bleed, never the
overnight headline. This card says "the toll booth is cheap tonight", not
"the coin will land your way".

DECISION-TIME LOG. The FIRST full (non-provisional) evaluation of each date
is appended to .closing_tonight.jsonl and never overwritten — so what the
panel said at decision time stays auditable against what the backtest later
computes for that night from synced bars. A read is FULL only from 15:05 IST:
between 15:00 and 15:05 the (15,0) candle is still forming — its open (the
signal print) is final, but the VIX close and the day's high/low keep moving
until the bar settles — so those minutes stay PROVISIONAL and are never
logged (review catch: a 15:01 read could have frozen a CLEAN that the settled
bar scores FLAGGED). From 15:05 the read is deterministic, and 15:05 is also
the study's fill clock, so the loggable card is exactly the one the backtest
grades. The row lands by itself: snapshot.capture_loop calls capture() from
15:05 (and again on its later ticks if Kite was not logged in at the time —
the settled read is the same card whenever it is taken), so a night is only
missed when the backend itself was down.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from app.closing import cpr, events, oiwall, tiers
from app.closing.calendar import IST, ExpiryCalendar
from app.closing.data import VIX_TOKEN, _in_session
from app.closing.pricing import atm_strike, dte_days
from app.closing.study import OPEN_BAR, SIGNAL_BAR, StudyConfig, build_days, close_ref
from app.config import get_settings
from app.closing.flags import MID_HI, MID_LO
from app.market import calendar as mcal
from app.patterns.data import NIFTY_TOKEN

log = logging.getLogger("tradewell.closing")

LOG_PATH = Path(__file__).resolve().parents[2] / ".closing_tonight.jsonl"
_FETCH_DAYS = 10          # enough tape to guarantee a previous session
_PROJECT_DAYS = 45        # forward calendar horizon for expiry resolution
# Round-4 live guidance: vol-expansion readings this close to zero flip on
# noise; the check still scores raw sign (the frozen definition) but the
# detail says so.
VIX_KNIFE_EDGE = 0.15

CHECK_LABELS = {
    "lasthr": "last hour agrees",
    "volexp": "vol expansion",
    "midrange": "off mid-range",
    "bridge": "no holiday bridge",
    "monthend": "not month-end",
}


SETTLE_HM = dtime(15, 5)   # the 15:00 bar stops forming here — also the fill clock


def calendar_covers(d: date) -> bool:
    """Whether mcal's holiday list actually covers `d`'s year. mcal itself
    fails OPEN on unknown years (any weekday counts as a trading day), which
    is right for its own callers but would let this card score a bridge or
    month-end check as definitively 'clear' from a guess — the unknown-never-
    passes rule says those checks go UNKNOWN instead (review catch)."""
    prefix = str(d.year)
    return any(h.startswith(prefix) for h in mcal.HOLIDAYS)


def next_trading_day(d: date, horizon: int = 15) -> Optional[date]:
    """First trading day strictly after `d`, from the market calendar.
    Fail-open on unknown years (weekday-only), same as mcal itself — pair
    with calendar_covers() before treating the answer as a fact."""
    probe = d
    for _ in range(horizon):
        probe += timedelta(days=1)
        if mcal.is_trading_day(probe):
            return probe
    return None


def _check(key: str, status: str, detail: str) -> dict:
    return {"key": key, "label": CHECK_LABELS[key], "status": status, "detail": detail}


def evaluate(days: dict, vix_days: dict, today: date, now: datetime,
             next_day: Optional[date], expiry: Optional[date],
             oi_chains: Optional[dict] = None) -> dict:
    """The pure read: today's bars -> values + five checks + verdict.

    `days` is study.build_days output; `vix_days` maps date -> {(h,m): close};
    `oi_chains` (optional) is the previous session's close OI per expiry for
    the OI-wall shadow — informational only, never a check.
    Before the 15:00 bar exists the read is PROVISIONAL: the latest close
    stands in for the 15:00 print and the card says so. Unknown inputs count
    as unknown, never as clear — a verdict of CLEAN requires all five checks
    strictly clear.
    """
    day = days.get(today)
    if day is None or not day.bars:
        return {"available": False,
                "reason": "No index bars for today yet — the session may not "
                          "have opened, or today is a holiday."}
    open_bar = day.bar(OPEN_BAR)
    if open_bar is None:
        return {"available": False, "reason": "Today's 09:15 bar is missing — "
                                              "cannot compute the day body."}

    sig = day.bar(SIGNAL_BAR)
    # The (15,0) candle EXISTS from 15:00:01 but keeps forming until 15:05 —
    # its open (the print) is final, its close/high/low are not. A read is
    # full only once the bar has settled; before that everything stays
    # provisional and unloggable, matching the study's own 15:05 fill clock.
    settle_at = datetime.combine(today, SETTLE_HM, tzinfo=IST)
    settled = now >= settle_at
    provisional = sig is None or not settled
    if sig is None:
        last_hm = max(day.bars)
        p1500 = day.bars[last_hm][3]
        signal_time = f"latest bar {last_hm[0]:02d}:{last_hm[1]:02d} (final read at 15:00)"
    elif not settled:
        p1500 = sig[0]
        signal_time = "the 15:00 print (checks settle at 15:05)"
    else:
        p1500 = sig[0]
        signal_time = "the 15:00 print"

    day_open = open_bar[0]
    body = p1500 - day_open
    direction = "CE" if body > 0 else "PE" if body < 0 else None
    # Shadow signal (closing/oiwall.py, registered 22-Aug): the previous
    # session's close OI walls on the held expiry. Same contract as CPR — a
    # chip and a logged value, never a check.
    ow = None
    if oi_chains:
        exp_key = oiwall.pick_expiry(oi_chains, expiry, today)
        if exp_key:
            ow = oiwall.walls(oi_chains[exp_key], p1500, direction)

    prev_sessions = [d for d in days if d < today]
    prev_close = close_ref(days[max(prev_sessions)]) if prev_sessions else None
    # Shadow signal (closing/cpr.py, registered 22-Aug): the previous
    # session's CPR. Informational chip only — it is not one of the five
    # checks and never touches the verdict.
    cp = cpr.from_session(days[max(prev_sessions)]) if prev_sessions else None

    checks = []

    # 1 — last hour agrees with the day body (signals.lasthr_confirm)
    b14 = day.bar((14, 0))
    if b14 is None:
        checks.append(_check("lasthr", "unknown", "No 14:00 bar yet — readable from ~14:05."))
    else:
        lh = p1500 - b14[0]
        if body == 0 or lh == 0:
            checks.append(_check("lasthr", "red",
                                 f"Body {body:+.1f} / last hour {lh:+.1f} pts — flat legs "
                                 "mean the tape is unconvinced; stand-aside night."))
        elif (body > 0) == (lh > 0):
            checks.append(_check("lasthr", "clear",
                                 f"Body {body:+.1f} pts and last hour {lh:+.1f} pts point "
                                 "the same way."))
        else:
            checks.append(_check("lasthr", "red",
                                 f"Body {body:+.1f} pts but the last hour ran {lh:+.1f} — "
                                 "the 14-Aug shape; following the body here scored "
                                 "negative in every window."))

    # 2 — vol expansion (overnight/filters, verified): VIX at 15:00 above its
    # prev close OR its own 09:15 open. Same unknown semantics as the flag:
    # any known positive leg fires; both known non-positive fails; else unknown.
    vbars = vix_days.get(today, {})
    v1500 = None
    for hm in ((15, 0), (14, 55), (14, 50), (14, 45)):
        if hm in vbars:
            v1500 = vbars[hm]
            break
    if v1500 is None and provisional and vbars:
        v1500 = vbars[max(vbars)]
    v0915 = vbars.get((9, 15))
    pv = vix_days.get(max(prev_sessions)) if prev_sessions else None
    v_prev_close = pv[max(pv)] if pv else None
    legs, parts = [], []
    if v_prev_close is not None and v1500 is not None:
        legs.append(round(v1500 - v_prev_close, 2))
        parts.append(f"vs prev close {v_prev_close:.2f} ({legs[-1]:+.2f})")
    if v0915 is not None and v1500 is not None:
        legs.append(round(v1500 - v0915, 2))
        parts.append(f"vs 09:15 {v0915:.2f} ({legs[-1]:+.2f})")
    edge = any(abs(x) <= VIX_KNIFE_EDGE for x in legs)
    detail = (f"India VIX {v1500:.2f} " + " / ".join(parts) if v1500 is not None and parts
              else "VIX legs unavailable.")
    if edge:
        detail += f" — knife-edge (within {VIX_KNIFE_EDGE}); Round-4 live guidance wants +0.1 clearance."
    if any(x > 0 for x in legs):
        checks.append(_check("volexp", "clear", detail))
    elif len(legs) == 2:
        checks.append(_check("volexp", "red", detail + " Vol is not being bid — theta is unsubsidised."))
    else:
        checks.append(_check("volexp", "unknown", detail))

    # 3 — off mid-range (overnight/filters, near-survivor): the print outside
    # the middle of today's range so far.
    seen = [day.bars[k] for k in sorted(day.bars) if k <= SIGNAL_BAR]
    hi = max(b[1] for b in seen)
    lo = min(b[2] for b in seen)
    if hi > lo:
        pos = (p1500 - lo) / (hi - lo)
        if MID_LO < pos < MID_HI:
            checks.append(_check("midrange", "red",
                                 f"Print at {pos:.2f} of today's {lo:.0f}-{hi:.0f} — a "
                                 "direction from mid-range is one the market has not "
                                 "committed to."))
        else:
            checks.append(_check("midrange", "clear",
                                 f"Print at {pos:.2f} of today's {lo:.0f}-{hi:.0f} range."))
    else:
        pos = None
        checks.append(_check("midrange", "unknown", "Zero range so far — position undefined."))

    # 4 — no holiday bridge (closing/attribution, registered): carry of
    # exactly 2 or 4+ days. A plain Fri->Mon weekend (3) is deliberately NOT
    # flagged — it flipped sign between backtest windows. The carry is a FACT
    # only where the holiday calendar has data; a fail-open weekday guess
    # (e.g. probing into a year whose NSE list is not loaded yet) makes both
    # calendar checks UNKNOWN, never silently clear.
    carry_known = (next_day is not None and calendar_covers(today)
                   and calendar_covers(next_day))
    carry = (next_day - today).days if next_day else None
    if not carry_known:
        why = ("Next trading day unresolved." if next_day is None else
               f"Holiday calendar does not cover {next_day.year} — the real "
               "carry is unknowable until the new NSE list is loaded.")
        checks.append(_check("bridge", "unknown", why))
    elif carry == 2 or carry > 3:
        checks.append(_check("bridge", "red",
                             f"Exit is {next_day} — {carry} calendar days of decay for one "
                             "night of edge (bridge losers ran -19%/-18% mean across the "
                             "two windows)."))
    else:
        note = " Plain weekend — extra theta, but not flagged (flipped between windows)." \
            if carry == 3 else ""
        checks.append(_check("bridge", "clear", f"Exit is {next_day} — carry {carry}d.{note}"))

    # 5 — not month-end (closing/attribution, registered)
    if not carry_known:
        checks.append(_check("monthend", "unknown",
                             "Next trading day unresolved." if next_day is None else
                             f"Holiday calendar does not cover {next_day.year}."))
    elif next_day.month != today.month:
        checks.append(_check("monthend", "red",
                             "Last trading session of the month (-18%/-21% mean in the two "
                             "windows, n=36 — the weaker, hypothesis-grade flag)."))
    else:
        checks.append(_check("monthend", "clear", "Not the month's last session."))

    red = [c["key"] for c in checks if c["status"] == "red"]
    if red:
        verdict = "FLAGGED"
    elif all(c["status"] == "clear" for c in checks):
        verdict = "CLEAN"
    else:
        verdict = "INCOMPLETE"

    # A card for a PAST session (weekend / holiday / pre-open fallback) is
    # priced at its own 15:05 decision clock, not at the moment of the read —
    # the DTE the user sees is the one the fill would have carried.
    stale = today < now.astimezone(IST).date()
    ev_tonight = events.tonight(today)
    now_ts = settle_at.timestamp() if stale else now.timestamp()
    return {
        "available": True,
        "stale": stale,
        "date": today.isoformat(),
        "weekday": today.strftime("%A"),
        "as_of": now.astimezone(IST).isoformat(timespec="seconds"),
        "provisional": provisional,
        "signal_time": signal_time,
        "values": {
            "day_open": round(day_open, 2),
            "p1400": round(b14[0], 2) if b14 else None,
            "p1500": round(p1500, 2),
            "gap_pts": round(body, 2),
            "direction": direction,
            "prev_close": round(prev_close, 2) if prev_close is not None else None,
            "strike": atm_strike(p1500) if direction else None,
            "expiry": expiry.isoformat() if expiry else None,
            "dte": round(dte_days(now_ts, expiry), 1) if expiry else None,
            "next_trading_day": next_day.isoformat() if next_day else None,
            "carry_days": carry,
            "vix_1500": round(v1500, 2) if v1500 is not None else None,
            "vix_prev_close": round(v_prev_close, 2) if v_prev_close is not None else None,
            "vix_0915": round(v0915, 2) if v0915 is not None else None,
            "range_pos": round(pos, 3) if pos is not None else None,
            "cpr_bc": cp["bc"] if cp else None,
            "cpr_tc": cp["tc"] if cp else None,
            "cpr_width_pts": cp["width_pts"] if cp else None,
            "cpr_width_pct": cp["width_pct"] if cp else None,
            "cpr_narrow": cp["narrow"] if cp else None,
            "oi_ce_wall": ow["ce_wall"] if ow else None,
            "oi_pe_wall": ow["pe_wall"] if ow else None,
            "oi_ahead_pts": ow["ahead_pts"] if ow else None,
            "oi_pcr": ow["pcr"] if ow else None,
            "oi_state": ow["state"] if ow else None,
            # Shadow flag (closing/events.py, registered 23-Aug): scheduled US
            # releases landing in tonight's hold window. Informational only.
            "events": ev_tonight,
            "event_tonight": (bool(ev_tonight)) if ev_tonight is not None else None,
        },
        "checks": checks,
        "red": red,
        "verdict": verdict,
        # Clean Gold / Silver / Bronze (closing/tiers.py): the verdict graded
        # by the OI-wall and CPR shadows. A label over the chips, never a
        # check; None when an input is unknown or the card is not CLEAN.
        "tier": tiers.tier(verdict, ow["state"] if ow else None, cp["narrow"] if cp else None),
        "tier_note": tiers.NOTES.get(
            tiers.tier(verdict, ow["state"] if ow else None, cp["narrow"] if cp else None)),
        "note": (
            "Five pre-registered checks read live — no new rules. The clean cell "
            "made money in both backtest windows, but the composite was assembled "
            "after seeing them: it is graded by live nights only, and a clean "
            "night still carried a ~21% chance of a >50% loss historically. The "
            "checks price the theta, not the overnight news."),
    }


# --- decision-time log -------------------------------------------------------

# /closing/tonight deliberately skips the module's sync/analyze single-flight
# lock, so two overlapping requests (a poll racing a manual refresh) can reach
# the log together; the check-then-append below must be atomic or both would
# write a "first" row (review catch).
_log_lock = threading.Lock()


def _first_logged(today: date) -> Optional[dict]:
    if not LOG_PATH.exists():
        return None
    try:
        with LOG_PATH.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue   # torn line — same tolerance as the other jsonl stores
                if row.get("date") == today.isoformat():
                    return row
    except OSError:
        return None
    return None


def logged_cards() -> list:
    """Every date's standing decision-time record (first row per date wins),
    oldest first — the raw material for grading the composite card on nights
    it had never seen."""
    if not LOG_PATH.exists():
        return []
    rows: dict = {}
    try:
        with LOG_PATH.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("date") and row["date"] not in rows:
                    rows[row["date"]] = row
    except OSError:
        return []
    return [rows[d] for d in sorted(rows)]


def log_first_eval(result: dict) -> Optional[dict]:
    """Append the FIRST full evaluation of the date; return the standing
    decision-time record (existing one wins — it is what the user saw)."""
    if not result.get("available") or result.get("provisional"):
        return _first_logged(date.fromisoformat(result["date"])) if result.get("date") else None
    with _log_lock:
        existing = _first_logged(date.fromisoformat(result["date"]))
        if existing:
            return existing
        row = {"date": result["date"], "as_of": result["as_of"],
               "verdict": result["verdict"], "red": result["red"],
               "values": result["values"]}
        try:
            with LOG_PATH.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            log.warning("Tonight log unwritable at %s", LOG_PATH)
        return row


# --- live orchestration ------------------------------------------------------

def capture(kite, now: Optional[datetime] = None) -> dict:
    """Loop entry point (snapshot.capture_loop): land today's decision-time
    card once the 15:00 bar has settled. Idempotent and cheap once logged —
    the Kite fetch only happens while today has no standing row."""
    now = now or datetime.now(IST)
    today = now.astimezone(IST).date()
    if not mcal.is_trading_day(today):
        return {"logged": False, "reason": "not a trading day"}
    if now.astimezone(IST).time() < SETTLE_HM:
        return {"logged": False, "reason": "before the 15:05 settle clock"}
    existing = _first_logged(today)
    if existing:
        return {"logged": False, "reason": "already logged", "row": existing}
    if kite is None:
        return {"logged": False, "reason": "kite not authenticated"}
    result = run(kite)
    first = result.get("first_eval")
    if result.get("available") and not result.get("stale") and first:
        log.info("15:05 card logged for %s: %s %s", today, first["verdict"], first["red"])
        return {"logged": True, "row": first}
    return {"logged": False,
            "reason": result.get("reason") or result.get("stale_reason") or "provisional"}


def _session_frame(candles: list) -> pd.DataFrame:
    rows = []
    for c in candles:
        dt = c["date"]
        if not _in_session(dt):
            continue
        rows.append((int(dt.timestamp()), c["open"], c["high"], c["low"], c["close"]))
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])


def card_date_for(today: date, days: dict, now: datetime) -> tuple:
    """Which session the card reads: today if its tape exists, else the most
    recent session in the window (a Saturday shows Friday's card; a Monday
    at 08:00 shows Friday's until the first bar prints). Returns
    (date, stale_reason) — stale_reason is None for today's own card."""
    if today in days and days[today].bars:
        return today, None
    past = [d for d in days if d < today]
    if not past:
        return None, None
    if not mcal.is_trading_day(today):
        why = f"No session today — {mcal.market_closed_reason(now)}."
    else:
        why = "Today's session has no bars yet."
    return max(past), why + " Showing the last session's card."


def run(kite) -> dict:
    """Fetch the tape (a light ~10-day window, in memory only — never touches
    the synced stores) and evaluate today's card — or, when today has no
    session yet, the last session's settled card."""
    now = datetime.now(IST)
    today = now.date()

    start = now - timedelta(days=_FETCH_DAYS)
    nifty = _session_frame(kite.historical_data(NIFTY_TOKEN, start, now, "5minute"))
    if nifty.empty:
        return {"available": False, "reason": "Kite returned no index bars for the window."}
    days = build_days(nifty)
    card_day, stale_reason = card_date_for(today, days, now)
    if card_day is None:
        return {"available": False,
                "reason": "No index bars for today, and no earlier session in the "
                          f"{_FETCH_DAYS}-day window to fall back to."}
    today = card_day

    vix_days: dict = {}
    for c in kite.historical_data(VIX_TOKEN, start, now, "5minute"):
        dt = c["date"]
        if _in_session(dt):
            vix_days.setdefault(dt.date(), {})[(dt.hour, dt.minute)] = float(c["close"])

    next_day = next_trading_day(today)
    settings = get_settings()
    cfg = StudyConfig()
    try:
        switch = date.fromisoformat(settings.closing_expiry_switch)
    except ValueError:
        switch = cfg.expiry_switch
    # Project only dates the holiday calendar actually covers — beyond that
    # ExpiryCalendar's own documented fallback (nominal weekday, no rollback)
    # applies, instead of fail-open weekdays masquerading as known sessions.
    projected = [d for i in range(1, _PROJECT_DAYS)
                 if calendar_covers(d := today + timedelta(days=i))
                 and mcal.is_trading_day(d)]
    cal = ExpiryCalendar(sorted(days) + projected, switch,
                         cfg.expiry_weekday_before, cfg.expiry_weekday_after)
    expiry = cal.holdable_expiry(today)

    # OI-wall shadow: the previous session's bhavcopy (cached after the first
    # fetch of the day). Unknown on any failure — the card never waits on it.
    prev_sessions = [d for d in days if d < today]
    oi_chains = oiwall.prev_close_chains(max(prev_sessions)) if prev_sessions else None

    result = evaluate(days, vix_days, today, now, next_day, expiry, oi_chains)
    if stale_reason:
        # The decision-time log records what the panel said AT 15:05 — a
        # weekend re-read of Friday is the same deterministic card, but it
        # is not what anyone saw at decision time, so it reports the logged
        # row (and any drift from it) without ever writing one.
        result["stale_reason"] = stale_reason
        first = _first_logged(today) if result.get("available") else None
    else:
        first = log_first_eval(result)
    if first and result.get("available") and not result.get("provisional"):
        result["first_eval"] = {"as_of": first["as_of"], "verdict": first["verdict"],
                                "red": first["red"]}
        if first["verdict"] != result["verdict"]:
            result["first_eval"]["drifted"] = True
    return result
