#!/usr/bin/env python3
"""The gold lab's three pre-registered rules — FROZEN 25-Aug-2026.

Parameters below were chosen from the climatology (gap base rates, range
magnitudes) BEFORE any outcome was graded, and are frozen: editing them
invalidates every graded sample and restarts the forward clock. New ideas =
new rule with a new name, never a tweak to these.

Everything downstream reuses simulate_day(): the historical ledger
(nightly_gold), the live TRIAL cards (gold_live), and the research page all
grade the SAME code path — no backtest-vs-live drift by construction.

Trades are GOLDM-sized (100g mini: ₹10 per point of the per-10g quote) with
approximate round-trip charges. Entries at bar closes; stop/target checked
against later bars' extremes, stop wins ties (conservative). Exit prices at
the level are idealised — no slippage model yet; the forward book will
measure how honest that is.
"""
from __future__ import annotations

FROZEN_AT = "2026-08-25"
CHARGES_RT = 250.0        # ₹ per GOLDM round trip (brokerage+CTT+exch, approx)
RUPEE_PER_POINT = 10.0    # GOLDM: 100g = 10 units of the per-10g quote

RULES = {
    "H1-gapfade": dict(
        label="Gap fade", qualify="overnight gap ≥ 0.5% either side",
        stop_pct=0.35, entry_after="09:15", exit_by="17:00"),
    "H2-burst": dict(
        label="09:00 burst continuation", qualify="09:00–09:30 move ≥ 0.15% either side",
        target_pct=0.30, stop_pct=0.25, entry_after="09:30", exit_by="15:00"),
    "H3-uswindow": dict(
        label="US-window follow", qualify="17:00→18:00 move ≥ 0.10% either side",
        target_pct=0.35, stop_pct=0.30, entry_after="18:00", exit_by="21:00"),
}


def _mins(ts: int) -> int:
    return ((ts + 19800) % 86400) // 60


def _close_trade(t: dict, price: float, ts: int, reason: str) -> dict:
    sign = 1 if t["dir"] == "LONG" else -1
    pct = sign * 100.0 * (price - t["entry"]) / t["entry"]
    t.update(exit=round(price, 2), exit_ts=ts, reason=reason,
             pct=round(pct, 3),
             rupees=round(pct / 100.0 * t["entry"] * RUPEE_PER_POINT
                          - CHARGES_RT))
    return t


def _run(bars, i0: int, direction: str, entry: float, ts: int, rule: str,
         target: float | None, stop: float, deadline_min: int,
         live: bool) -> dict:
    t = dict(rule=rule, dir=direction, entry=round(entry, 2), entry_ts=ts)
    sign = 1 if direction == "LONG" else -1
    stop_p = entry * (1 - sign * stop / 100.0)
    targ_p = entry * (1 + sign * target / 100.0) if target else None
    last = None
    for b in bars[i0 + 1:]:
        bts, _, hi, lo, close = b
        if _mins(bts) >= deadline_min:
            break
        last = b
        hit_stop = lo <= stop_p if sign > 0 else hi >= stop_p
        hit_targ = targ_p is not None and (
            hi >= targ_p if sign > 0 else lo <= targ_p)
        if hit_stop:                      # ties: stop first, conservative
            return _close_trade(t, stop_p, bts, "stop")
        if hit_targ:
            return _close_trade(t, targ_p, bts, "target")
    if live:
        if last is not None and _mins(last[0]) < deadline_min - 3:
            t.update(exit=None, last=last[4])
            return t                       # still open on the live tape
        if last is None:
            t.update(exit=None, last=entry)
            return t
    return _close_trade(t, (last or bars[i0])[4],
                        (last or bars[i0])[0], "time")


def simulate_day(bars: list, prev_close: float | None,
                 live: bool = False) -> list[dict]:
    """bars: [[ts, o, h, l, c], …] one MCX session, 3m, ascending."""
    out = []
    if len(bars) < 3:
        return out

    def first_at(minute: int) -> int | None:
        for i, b in enumerate(bars):
            if _mins(b[0]) >= minute:
                return i
        return None

    # H1 gap fade — against the overnight gap, target = yesterday's close.
    if prev_close:
        gap = 100.0 * (bars[0][1] - prev_close) / prev_close
        i = first_at(9 * 60 + 15)
        if abs(gap) >= 0.5 and i is not None:
            d = "SHORT" if gap > 0 else "LONG"
            e = bars[i][4]
            tpct = abs(100.0 * (prev_close - e) / e)
            out.append(_run(bars, i, d, e, bars[i][0], "H1-gapfade",
                            tpct or None, 0.35, 17 * 60, live))

    # H2 burst continuation — with the 09:00–09:30 move.
    w = [b for b in bars if 540 <= _mins(b[0]) < 570]
    i = first_at(570)
    if w and i is not None:
        mv = 100.0 * (w[-1][4] - w[0][1]) / w[0][1]
        if abs(mv) >= 0.15:
            out.append(_run(bars, i, "LONG" if mv > 0 else "SHORT",
                            bars[i][4], bars[i][0], "H2-burst",
                            0.30, 0.25, 15 * 60, live))

    # H3 US-window follow — with the 17:00→18:00 move.
    w = [b for b in bars if 17 * 60 <= _mins(b[0]) < 18 * 60]
    i = first_at(18 * 60)
    if w and i is not None:
        mv = 100.0 * (w[-1][4] - w[0][1]) / w[0][1]
        if abs(mv) >= 0.10:
            out.append(_run(bars, i, "LONG" if mv > 0 else "SHORT",
                            bars[i][4], bars[i][0], "H3-uswindow",
                            0.35, 0.30, 21 * 60, live))
    return out


def simulate_all(db) -> list[dict]:
    """Deterministic ledger over every stored MCX session (GOLD_FUT 3m).
    The candle DB is the truth; this output is only a cache of it."""
    rows = db.execute(
        "SELECT bar_ts, open, high, low, close FROM candles "
        "WHERE symbol='GOLD_FUT' AND tf='3m' ORDER BY bar_ts").fetchall()
    days: dict[int, list] = {}
    for r in rows:
        days.setdefault((r[0] + 19800) // 86400, []).append(list(r))
    day_close = {d: b[-1][4] for d, b in days.items()}
    trades = []
    prev = None
    for d in sorted(days):
        pc = day_close.get(prev) if prev is not None else None
        for t in simulate_day(days[d], pc):
            t["day"] = d
            trades.append(t)
        prev = d
    return trades
