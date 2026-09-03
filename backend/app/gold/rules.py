"""The three frozen gold rules + the ONE simulation code path.

Pre-registration contract (spec §4, frozen 25-Aug-2026): parameters live here
as constants and deliberately NOT in config.py — an .env-tunable parameter is
an editable one, and a parameter edited after grading kills the experiment.
A new idea gets a NEW rule key with its own freeze date; these three keep
theirs forever.

simulate_day() is the single code path (spec §5) that produces the historical
backtest, the ledger rebuild AND the live open-card state (live=True) — there
is no second implementation for backtest-vs-live drift to hide in. The whole
ledger is a deterministic cache of raw candles.

Simulation semantics (identical everywhere):
  * A 3m bar's ts is its OPEN; it closes at ts+180s. "Price at HH:MM" is the
    close of the last bar closing at or before HH:MM; a window opening at the
    session start reads the first bar's open.
  * Entries at bar closes: the first bar closing at/after the rule's entry
    time. Stop/target are tested against SUBSEQUENT bars' high/low; if both
    are touched inside one bar, the STOP wins (conservative tie-break). Time
    exit at the deadline's last close.
  * Exits are idealised (filled at the level, no slippage model yet) — the
    caveat ships in the results and the forward book's job includes measuring
    how honest it is.
  * Sizing: GOLDM mini (100g) = ₹10 per point of the per-10g quote; ~₹250
    round-trip charges per closed trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# Rules written, parameterized and frozen on this date. Days at/before it are
# backtest (hindsight-risked homework), days strictly after are forward (the
# blind exam). The freeze day itself lands in BACKTEST on purpose: a day
# partially observed at freeze time cannot be blind.
FREEZE_DATE = date(2026, 8, 25)

BAR_SECONDS = 180
RS_PER_POINT = 10.0     # GOLDM mini (100g): ₹10 per point of the per-10g quote
CHARGES_RT_RS = 250.0   # assumed round-trip charges, both legs

# Forward-sample gates (spec §5, same as the house rules).
MIN_FORWARD_SAMPLES = 30
LIVE_MONEY_BAR = (
    "100+ forward episodes, positive net expectancy after all charges, "
    "clustered-by-day CI above zero, profit factor >1.2, no single day >25% "
    "of profit, 2+ regimes.")


@dataclass(frozen=True)
class Rule:
    key: str
    name: str
    signal_kind: str            # "gap" (vs prev close) | "window" (intraday move)
    qualify_pct: float          # |signal| must be >= this, in %
    win_from: Optional[tuple]   # window rules: (h, m) the move is measured FROM
    win_to: Optional[tuple]     # window rules: (h, m) the move is measured TO
    against: bool               # True = fade the signal move, False = follow it
    entry_after: tuple          # (h, m): enter at the first bar CLOSING at/after
    target_pct: Optional[float]  # None = target is the gap origin (prev close)
    stop_pct: float
    exit_by: tuple              # (h, m): time exit at the last close at/before
    blurb: str


RULES = (
    Rule("H1", "Gap-fade", "gap", 0.50, None, None, True, (9, 15),
         None, 0.35, (17, 0),
         "|overnight gap| ≥ 0.5% → fade it from the first 3m close ≥ 09:15, "
         "target the previous close (gap fill), stop 0.35%, flat by 17:00."),
    Rule("H2", "Burst-follow", "window", 0.15, (9, 0), (9, 30), False, (9, 30),
         0.30, 0.25, (15, 0),
         "|09:00→09:30 move| ≥ 0.15% → follow it from the first close ≥ 09:30, "
         "target 0.30%, stop 0.25%, flat by 15:00."),
    Rule("H3", "US-window follow", "window", 0.10, (17, 0), (18, 0), False, (18, 0),
         0.35, 0.30, (21, 0),
         "|17:00→18:00 move| ≥ 0.10% → follow it from the first close ≥ 18:00, "
         "target 0.35%, stop 0.30%, flat by 21:00."),
)


def rule_table() -> list:
    """The frozen parameters, verbatim, for the results JSON and the tab."""
    out = []
    for r in RULES:
        out.append({
            "key": r.key, "name": r.name, "blurb": r.blurb,
            "qualify_pct": r.qualify_pct,
            "direction": "against" if r.against else "with",
            "entry_after": "%02d:%02d" % r.entry_after,
            "target": "prev close" if r.target_pct is None else f"{r.target_pct}%",
            "stop_pct": r.stop_pct,
            "exit_by": "%02d:%02d" % r.exit_by,
        })
    return out


# ---- bar helpers -------------------------------------------------------------

def _hm(ts: int) -> tuple:
    d = datetime.fromtimestamp(ts, IST)
    return (d.hour, d.minute)


def _close_hm(bar: dict) -> tuple:
    return _hm(int(bar["ts"]) + BAR_SECONDS)


def _px_at(bars: list, hm: tuple) -> Optional[dict]:
    """The last bar CLOSING at or before hm — the 'print at HH:MM'."""
    hit = None
    for b in bars:
        if _close_hm(b) <= hm:
            hit = b
        else:
            break
    return hit


def _ref_px(bars: list, hm: tuple) -> Optional[float]:
    """Window-start price: the close at hm, or the session's first OPEN when
    the window opens at/before the first bar (there is no close at 09:00)."""
    b = _px_at(bars, hm)
    if b is not None:
        return float(b["close"])
    if bars and hm <= _hm(int(bars[0]["ts"])):
        return float(bars[0]["open"])
    return None


def _entry_bar(bars: list, hm: tuple) -> Optional[int]:
    """Index of the first bar closing at/after hm."""
    for i, b in enumerate(bars):
        if _close_hm(b) >= hm:
            return i
    return None


# ---- the one code path -------------------------------------------------------

def simulate_day(bars: list, prev_close: Optional[float], live: bool = False) -> list:
    """One session of 3m bars (ascending dicts: ts/open/high/low/close) →
    one card per rule. `live=True` reports still-forming windows as `pending`
    and unresolved positions as `open`; the backtest resolves everything."""
    return [_simulate_rule(r, bars, prev_close, live) for r in RULES]


def _card(rule: Rule, bars: list, state: str, **kw) -> dict:
    base = {
        "rule": rule.key, "name": rule.name, "state": state,
        "date": (datetime.fromtimestamp(int(bars[0]["ts"]), IST).date().isoformat()
                 if bars else None),
        "signal_pct": None, "qualified": False, "direction": None,
        "entry_ts": None, "entry_px": None, "target_px": None, "stop_px": None,
        "exit_ts": None, "exit_px": None, "exit_reason": None,
        "points": None, "gross_rs": None, "net_rs": None,
        "last_px": float(bars[-1]["close"]) if bars else None,
        "note": None,
    }
    base.update(kw)
    return base


def _simulate_rule(rule: Rule, bars: list, prev_close: Optional[float],
                   live: bool) -> dict:
    if not bars:
        return _card(rule, bars, "no_data", note="no bars for the session")

    # 1. The signal.
    if rule.signal_kind == "gap":
        if not prev_close:
            return _card(rule, bars, "no_data", note="no previous close in the store")
        signal_pct = (float(bars[0]["open"]) - prev_close) / prev_close * 100.0
    else:
        to_bar = _px_at(bars, rule.win_to)
        # The window-end print is only FINAL once the tape has reached the
        # window end — before that, the last close is a forming value, and
        # reading it as the signal would let the live path qualify a rule
        # minutes before the backtest ever could (drift by early peeking).
        reached = _close_hm(bars[-1]) >= rule.win_to
        if to_bar is None or not reached:
            state = "pending" if live else "no_data"
            return _card(rule, bars, state,
                         note="signal window %02d:%02d→%02d:%02d still forming"
                              % (rule.win_from + rule.win_to))
        from_px = _ref_px(bars, rule.win_from)
        if not from_px:
            return _card(rule, bars, "no_data", note="no window-start print")
        signal_pct = (float(to_bar["close"]) - from_px) / from_px * 100.0

    signal_pct = round(signal_pct, 3)
    if abs(signal_pct) < rule.qualify_pct:
        return _card(rule, bars, "no_setup", signal_pct=signal_pct,
                     note=f"|{signal_pct}%| below the {rule.qualify_pct}% gate")

    sign = 1 if signal_pct > 0 else -1
    direction = -sign if rule.against else sign

    # 2. The entry: first bar closing at/after entry_after, filled at its close.
    ei = _entry_bar(bars, rule.entry_after)
    if ei is None:
        state = "pending" if live else "no_data"
        return _card(rule, bars, state, signal_pct=signal_pct, qualified=True,
                     direction="LONG" if direction > 0 else "SHORT",
                     note="qualified — awaiting the %02d:%02d entry print" % rule.entry_after)
    entry = bars[ei]
    if _close_hm(entry) > rule.exit_by:
        # Data hole: the first print at/after the entry time lands beyond the
        # exit deadline — there is no window left to trade.
        return _card(rule, bars, "no_data", signal_pct=signal_pct, qualified=True,
                     note="first entry print lands after the exit deadline")
    entry_px = float(entry["close"])
    if rule.target_pct is None:
        target_px = float(prev_close)          # H1: the gap origin
    else:
        target_px = entry_px * (1 + direction * rule.target_pct / 100.0)
    stop_px = entry_px * (1 - direction * rule.stop_pct / 100.0)
    if rule.target_pct is None and direction * (target_px - entry_px) <= 0:
        # Gap-fade with nothing left to fade: the tape crossed the previous
        # close before the entry print, so the gap-fill "target" sits at or
        # behind the entry. Entering would let any later touch of that level
        # book a LOSS labelled "target" (review catch) — the rule's fade is
        # already over, so the day is a stand-aside.
        return _card(rule, bars, "no_setup", signal_pct=signal_pct, qualified=True,
                     direction="LONG" if direction > 0 else "SHORT",
                     note="gap already filled before the entry print — no fade left")

    common = dict(
        signal_pct=signal_pct, qualified=True,
        direction="LONG" if direction > 0 else "SHORT",
        entry_ts=int(entry["ts"]), entry_px=round(entry_px, 2),
        target_px=round(target_px, 2), stop_px=round(stop_px, 2),
    )

    # 3. Stop/target against subsequent bars, stop winning any shared bar;
    #    then the time exit at the deadline's last close.
    last_in_window = None
    for b in bars[ei + 1:]:
        if _close_hm(b) > rule.exit_by:
            break
        last_in_window = b
        hi, lo = float(b["high"]), float(b["low"])
        if direction > 0:
            hit_stop, hit_target = lo <= stop_px, hi >= target_px
        else:
            hit_stop, hit_target = hi >= stop_px, lo <= target_px
        if hit_stop or hit_target:
            px = stop_px if hit_stop else target_px
            return _closed(rule, bars, common, int(b["ts"]), px,
                           "stop" if hit_stop else "target", direction, entry_px)

    deadline_passed = _close_hm(bars[-1]) >= rule.exit_by
    if last_in_window is not None and (deadline_passed or not live):
        return _closed(rule, bars, common, int(last_in_window["ts"]),
                       float(last_in_window["close"]), "time", direction, entry_px)
    if not live or deadline_passed:
        # Entry bar exists but no later bar closed inside the window — a data
        # hole. The backtest never called this a trade, so the live path past
        # the deadline must render the same verdict instead of an "open" card
        # that nothing can ever close (review catch).
        return _card(rule, bars, "no_data",
                     note="no closed bar inside the exit window after entry", **common)

    live_px = float(bars[-1]["close"])
    points = direction * (live_px - entry_px)
    return _card(rule, bars, "open", note="paper position open — marked to the last close",
                 points=round(points, 2),
                 gross_rs=round(points * RS_PER_POINT, 0),
                 net_rs=round(points * RS_PER_POINT - CHARGES_RT_RS, 0),
                 **common)


def _closed(rule: Rule, bars: list, common: dict, exit_ts: int, exit_px: float,
            reason: str, direction: int, entry_px: float) -> dict:
    points = direction * (exit_px - entry_px)
    return _card(rule, bars, "closed",
                 exit_ts=exit_ts, exit_px=round(exit_px, 2), exit_reason=reason,
                 points=round(points, 2),
                 gross_rs=round(points * RS_PER_POINT, 0),
                 net_rs=round(points * RS_PER_POINT - CHARGES_RT_RS, 0),
                 **common)


# ---- ledger over the stored spine -------------------------------------------

def phase_of(day_iso: str) -> str:
    return "backtest" if date.fromisoformat(day_iso) <= FREEZE_DATE else "forward"


def run_ledger(frame) -> list:
    """Every stored session through simulate_day (live=False), stamped with its
    phase. `frame`: the 3m spine DataFrame (ts ascending). The CURRENT session
    is excluded — a partial day is the live card's job, never the ledger's."""
    if frame is None or len(frame) == 0:
        return []
    today = datetime.now(IST).date().isoformat()
    by_day: dict = {}
    for row in frame.itertuples(index=False):
        d = datetime.fromtimestamp(int(row.ts), IST).date().isoformat()
        by_day.setdefault(d, []).append({
            "ts": int(row.ts), "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close)})
    out = []
    prev_close = None
    for d in sorted(by_day):
        bars = by_day[d]
        if d != today:
            for card in simulate_day(bars, prev_close, live=False):
                card["phase"] = phase_of(d)
                out.append(card)
        prev_close = float(bars[-1]["close"])
    return out


def summarise(trades: list) -> dict:
    """Closed-trade stats for one (rule, phase) bucket. Same summariser for
    every bucket so no number is ever computed two ways."""
    closed = [t for t in trades if t["state"] == "closed"]
    n = len(closed)
    if not n:
        return {"n": 0}
    nets = [t["net_rs"] for t in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    by_day: dict = {}
    for t in closed:
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + t["net_rs"]
    total = sum(nets)
    best_day = max(by_day.values())
    out = {
        "n": n,
        "win_rate_pct": round(100.0 * len(wins) / n, 1),
        "gross_rs": round(sum(t["gross_rs"] for t in closed), 0),
        "net_rs": round(total, 0),
        "avg_net_rs": round(total / n, 0),
        "best_rs": round(max(nets), 0),
        "worst_rs": round(min(nets), 0),
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses and sum(losses) != 0 else None),
        "exit_mix": {r: sum(1 for t in closed if t["exit_reason"] == r)
                     for r in ("target", "stop", "time")},
        # The no-single-day-carries-it gate needs this share; only meaningful
        # when the bucket is net positive.
        "best_day_share_pct": (round(100.0 * best_day / total, 1)
                               if total > 0 and best_day > 0 else None),
        "from": min(t["date"] for t in closed),
        "to": max(t["date"] for t in closed),
    }
    return out
