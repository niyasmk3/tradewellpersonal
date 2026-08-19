"""The Closing Day backtest: signal at 15:00, exit 09:50 the next morning.

THE RULE UNDER TEST (stated once, implemented once):
    At 15:00 IST on day D, compare NIFTY to day D-1's close.
      lower  -> buy the ATM PE
      higher -> buy the ATM CE
    Sell at 09:50 on the next trading day.

WHAT IS REAL AND WHAT IS MODELLED
    Layer 1 is the index itself: the 15:00 print, the previous close, and the
    09:50 print the next morning. Every number in `underlying` comes from real
    5-min bars and survives any argument about option pricing. If the signal
    has no edge there, nothing downstream can rescue it.
    Layer 2 is the option P&L, priced by app/closing/pricing.py. Read it with
    the error bars app/closing/validate.py publishes.

EXECUTION MODEL, and why it is not the flattering version
    The signal is read from the OPEN of the 15:00 bar — the 15:00 print itself.
    The fill happens at that bar's CLOSE, five minutes later. Reading and
    filling at the same instant would be a look-ahead-free but physically
    impossible trade; this version pays for the time it takes to act on what
    you saw. The exit is the 09:50 bar's open, the 09:50 print.

    Entry pays half the measured ATM bid-ask above mid; the exit gives half
    back. Zerodha's full charge schedule is applied through app/paper/charges,
    the same code the paper book books its P&L with.

PREVIOUS CLOSE, and the CAS trap
    Since 03-Aug-2026 NSE runs a Closing Auction Session: NIFTY freezes around
    15:15 and a separate auction print lands at ~15:25 that is NOT the close.
    Taking the last bar blindly would have read 17-Aug as 24287 when the close
    was 24338 — a 50-point error, easily enough to flip a signal. The freeze is
    detected structurally (a flat bar at/after 15:15 means the tape has stopped)
    rather than by hardcoded date, so pre-CAS days are unaffected and a future
    change in auction timing does not silently corrupt the series.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from app.closing.calendar import ExpiryCalendar, ist_dt, THURSDAY, TUESDAY
from app.closing.pricing import OptionModel, atm_strike, dte_days
from app.paper.charges import charges

# The 5-min bars the study reads, as IST (hour, minute) bar-open keys.
SIGNAL_BAR = (15, 0)     # its OPEN is the 15:00 print; its CLOSE is the fill
EXIT_BAR = (9, 50)       # its OPEN is the 09:50 print
OPEN_BAR = (9, 15)       # its OPEN is the session open
FREEZE_FROM = (15, 15)   # a flat bar at/after this is the CAS freeze

# Which 15:00 reference decides CE vs PE.
#   day_open    today's own body: is the 15:00 print above where today opened.
#   prev_close  the original hypothesis: today vs yesterday's close.
#
# DAY_OPEN IS THE DEFAULT, and the reason is the always-CE control in
# app/closing/signals.py. NIFTY drifts up ~7 points a night, so "vs prev close"
# — which picks CE on 54% of nights — collects part of that simply for being net
# long: over three years it scored +7.6 pts against always-CE's +6.9, a
# difference indistinguishable from nothing. "vs day open" splits ~48/52, so it
# is drift-neutral and whatever it earns, it earns from being right: skill +15.1
# vs +7.0, holding on both the in-sample and holdout windows.
#
# This is a change to the HYPOTHESIS, not a tuned parameter, so both modes stay
# implemented and results carry a side-by-side comparison of the two.
# See docs/closing-day-strategy-2026-08-19.md.
SIGNAL_PREV_CLOSE = "prev_close"
SIGNAL_DAY_OPEN = "day_open"
SIGNAL_MODES = (SIGNAL_DAY_OPEN, SIGNAL_PREV_CLOSE)
DEFAULT_SIGNAL_MODE = SIGNAL_DAY_OPEN


@dataclass
class StudyConfig:
    lots: int = 1
    lot_size: int = 65              # NIFTY, per the live Kite instrument dump
    strike_step: float = 50.0
    entry_spread_pct: float = 0.0024   # measured ATM spread, ~15:00 window
    exit_spread_pct: float = 0.0026    # measured ATM spread, ~09:50 window
    expiry_switch: date = date(2025, 9, 2)
    expiry_weekday_before: int = THURSDAY
    expiry_weekday_after: int = TUESDAY
    signal_mode: str = DEFAULT_SIGNAL_MODE
    deadband_pts: float = 0.0       # |15:00 - reference| below this = no trade
    bootstrap_iters: int = 2000
    # The VIX range the option model was actually calibrated over. Trades
    # outside it are extrapolation, and the study says how much of its own
    # P&L depends on them rather than leaving the reader to assume none does.
    calib_vix_lo: Optional[float] = None
    calib_vix_hi: Optional[float] = None
    # Correction for the model's own measured overnight bias, in percentage
    # points of premium. validate.overnight_check() finds the model understates
    # the real 15:00->09:50 move; leaving that out would report a pessimism the
    # evidence does not support, and applying it silently would report an
    # optimism it barely supports. So it is a separate, labelled run.
    bias_adjust_pp: float = 0.0

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size


@dataclass
class Day:
    d: date
    bars: dict = field(default_factory=dict)   # (h, m) -> (open, high, low, close, ts)

    def bar(self, hm):
        return self.bars.get(hm)


def build_days(spine: pd.DataFrame) -> "dict[date, Day]":
    days: dict = {}
    for ts, o, h, l, c in zip(spine["ts"], spine["open"], spine["high"],
                              spine["low"], spine["close"]):
        dt = ist_dt(int(ts))
        day = days.get(dt.date())
        if day is None:
            day = days[dt.date()] = Day(dt.date())
        day.bars[(dt.hour, dt.minute)] = (float(o), float(h), float(l), float(c), int(ts))
    return days


def close_ref(day: Day) -> Optional[float]:
    """The session's last free-tape price — the number a trader reads as the
    close, with the CAS freeze and its auction print stepped over."""
    keys = sorted(day.bars)
    if not keys:
        return None
    freeze_at = None
    for hm in keys:
        if hm < FREEZE_FROM:
            continue
        o, h, l, c, _ts = day.bars[hm]
        if o == h == l == c:     # tape stopped: a whole 5-min bar with no range
            freeze_at = hm
            break
    if freeze_at is None:
        return day.bars[keys[-1]][3]
    idx = keys.index(freeze_at)
    if idx == 0:
        return None
    return day.bars[keys[idx - 1]][3]


class VixLookup:
    def __init__(self, vix: pd.DataFrame) -> None:
        self._m = dict(zip(vix["ts"].astype(int), vix["close"].astype(float)))

    def at(self, ts: int) -> Optional[float]:
        base = ts - (ts % 300)
        for cand in (base, base - 300, base - 600, base - 900):
            v = self._m.get(cand)
            if v:
                return v
        return None


def _month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _dte_bucket(dte: float) -> str:
    if dte < 1.5:
        return "0-1"
    if dte < 3.5:
        return "2-3"
    if dte < 5.5:
        return "4-5"
    return "6-7"


def _vix_bucket(v: float) -> str:
    if v < 12:
        return "<12"
    if v < 15:
        return "12-15"
    if v < 20:
        return "15-20"
    return ">20"


def _gap_bucket(pts: float) -> str:
    a = abs(pts)
    if a < 25:
        return "<25"
    if a < 50:
        return "25-50"
    if a < 100:
        return "50-100"
    return ">100"


def build_trades(days: "dict[date, Day]", vix: VixLookup, model: OptionModel,
                 cfg: StudyConfig, start: Optional[date] = None) -> tuple:
    """Returns (trades, skipped). One trade per eligible day, in date order."""
    ordered = sorted(days)
    cal = ExpiryCalendar(ordered, cfg.expiry_switch,
                         cfg.expiry_weekday_before, cfg.expiry_weekday_after)
    trades, skipped = [], defaultdict(int)

    for i in range(1, len(ordered) - 1):
        d = ordered[i]
        if start and d < start:
            continue
        prev_d, next_d = ordered[i - 1], ordered[i + 1]
        day, prev_day, next_day = days[d], days[prev_d], days[next_d]

        sig_bar = day.bar(SIGNAL_BAR)
        exit_bar = next_day.bar(EXIT_BAR)
        open_bar = day.bar(OPEN_BAR)
        b1400 = day.bar((14, 0))
        pclose = close_ref(prev_day)
        if sig_bar is None:
            skipped["no 15:00 bar"] += 1
            continue
        if exit_bar is None:
            skipped["no 09:50 bar next day"] += 1
            continue
        if pclose is None:
            skipped["no previous close"] += 1
            continue
        if cfg.signal_mode == SIGNAL_DAY_OPEN and open_bar is None:
            skipped["no 09:15 bar"] += 1
            continue

        sig_price, entry_spot, entry_ts = sig_bar[0], sig_bar[3], sig_bar[4]
        if cfg.signal_mode == SIGNAL_PREV_CLOSE:
            reference = pclose
        elif cfg.signal_mode == SIGNAL_DAY_OPEN:
            reference = open_bar[0]
        else:
            raise ValueError(f"unknown signal_mode {cfg.signal_mode!r} — "
                             f"expected one of {SIGNAL_MODES}")
        gap = sig_price - reference
        if abs(gap) <= cfg.deadband_pts:
            skipped["inside deadband"] += 1
            continue
        is_call = gap > 0

        expiry = cal.holdable_expiry(d)
        if expiry is None or expiry < next_d:
            skipped["no holdable expiry"] += 1
            continue

        exit_spot, exit_ts = exit_bar[0], exit_bar[4]
        v_in, v_out = vix.at(entry_ts), vix.at(exit_ts)
        if v_in is None or v_out is None:
            skipped["no VIX"] += 1
            continue

        strike = atm_strike(entry_spot, cfg.strike_step)
        dte_entry = dte_days(entry_ts, expiry)
        mid_in = model.premium(entry_spot, strike, entry_ts, expiry, is_call, v_in)
        mid_out = model.premium(exit_spot, strike, exit_ts, expiry, is_call, v_out)
        if not mid_in or mid_out is None or mid_in <= 0:
            skipped["unpriceable"] += 1
            continue

        if cfg.bias_adjust_pp:
            mid_out = max(mid_out + mid_in * cfg.bias_adjust_pp / 100.0, 0.0)
        fill_in = round(mid_in * (1 + cfg.entry_spread_pct / 2), 2)
        fill_out = round(max(mid_out * (1 - cfg.exit_spread_pct / 2), 0.05), 2)
        qty = cfg.qty
        gross = (fill_out - fill_in) * qty
        cost = charges(fill_in, fill_out, qty, 2)
        net = gross - cost

        trades.append({
            "date": d.isoformat(),
            "month": _month(d),
            "exit_date": next_d.isoformat(),
            "prev_close": round(pclose, 2),
            "day_open": round(open_bar[0], 2) if open_bar else None,
            "p1400": round(b1400[0], 2) if b1400 else None,
            "reference": round(reference, 2),
            "signal_mode": cfg.signal_mode,
            "signal_price": round(sig_price, 2),
            "gap_pts": round(gap, 2),
            "direction": "CE" if is_call else "PE",
            "entry_spot": round(entry_spot, 2),
            "exit_spot": round(exit_spot, 2),
            "spot_move_pts": round(exit_spot - entry_spot, 2),
            # Positive = the index moved the way the signal pointed.
            "signed_move_pts": round((exit_spot - entry_spot) * (1 if is_call else -1), 2),
            "strike": strike,
            "expiry": expiry.isoformat(),
            "dte_entry": round(dte_entry, 2),
            "dte_bucket": _dte_bucket(dte_entry),
            "gap_bucket": _gap_bucket(gap),
            "vix_bucket": _vix_bucket(v_in),
            # Flagged, not dropped: an extrapolated night is still a real
            # night. summarise() reports what share of the P&L they carry.
            "extrapolated": bool(
                (cfg.calib_vix_lo is not None and v_in < cfg.calib_vix_lo)
                or (cfg.calib_vix_hi is not None and v_in > cfg.calib_vix_hi)),
            "vix_in": round(v_in, 2),
            "vix_out": round(v_out, 2),
            "mid_in": mid_in,
            "mid_out": mid_out,
            "fill_in": fill_in,
            "fill_out": fill_out,
            "gross_pct": round((fill_out - fill_in) / fill_in * 100, 2),
            "net_pct": round(net / (fill_in * qty) * 100, 2),
            "gross_rs": round(gross, 2),
            "charges_rs": round(cost, 2),
            "net_rs": round(net, 2),
            "qty": qty,
        })
    return trades, dict(skipped)


def _pct_stats(values: list) -> dict:
    if not values:
        return {"n": 0}
    wins = [v for v in values if v > 0]
    return {
        "n": len(values),
        "win_rate_pct": round(len(wins) / len(values) * 100, 1),
        "mean_pct": round(statistics.mean(values), 2),
        "median_pct": round(statistics.median(values), 2),
        "best_pct": round(max(values), 1),
        "worst_pct": round(min(values), 1),
        "stdev_pct": round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0,
    }


def _bootstrap_ci(values: list, iters: int, seed: int = 12345) -> Optional[list]:
    """95% CI of the mean by resampling. Deterministic seed so a re-run of the
    analysis does not quietly move the confidence interval."""
    n = len(values)
    if n < 20:
        return None
    rnd = _Lcg(seed)
    means = []
    for _ in range(iters):
        total = 0.0
        for _j in range(n):
            total += values[rnd.next_int(n)]
        means.append(total / n)
    means.sort()
    return [round(means[int(iters * 0.025)], 2), round(means[int(iters * 0.975)], 2)]


class _Lcg:
    """Tiny deterministic PRNG — avoids seeding the global `random` module,
    which other parts of the process may rely on."""

    def __init__(self, seed: int) -> None:
        self.s = seed & 0xFFFFFFFF

    def next_int(self, n: int) -> int:
        self.s = (1103515245 * self.s + 12345) & 0x7FFFFFFF
        return self.s % n


def _drawdown(series: list) -> dict:
    peak, worst, cum = 0.0, 0.0, 0.0
    for x in series:
        cum += x
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return {"final_rs": round(cum, 2), "max_drawdown_rs": round(worst, 2)}


# Bucket labels are ordinal, not alphabetical: sorted() puts "<25" between
# "25-50" and ">100", which reads as noise in a table whose whole point is a
# monotone trend.
_BUCKET_ORDER = {
    "dte_bucket": ["0-1", "2-3", "4-5", "6-7"],
    "gap_bucket": ["<25", "25-50", "50-100", ">100"],
    "vix_bucket": ["<12", "12-15", "15-20", ">20"],
    "direction": ["CE", "PE"],
}


def _group(trades: list, key: str, field_name: str = "net_pct") -> dict:
    out = defaultdict(list)
    for t in trades:
        out[t[key]].append(t[field_name])
    order = _BUCKET_ORDER.get(key)
    keys = ([k for k in order if k in out] + sorted(k for k in out if k not in order)
            if order else sorted(out))
    return {k: _pct_stats(out[k]) for k in keys}


def _trust(trades: list, cfg: StudyConfig) -> dict:
    """How much of the modelled result rests on volatility the model never saw.

    The option model was fitted in one calm week. Most of this study's P&L
    swing sits in high-VIX days it has no evidence about, and that fraction
    belongs next to the headline rather than in a footnote.
    """
    if cfg.calib_vix_lo is None or cfg.calib_vix_hi is None:
        return {"available": False,
                "reason": "no calibration sample — pricing is uncalibrated throughout"}
    out = [t for t in trades if t.get("extrapolated")]
    gross_abs = sum(abs(t["net_rs"]) for t in trades) or 1.0
    return {
        "available": True,
        "calibrated_vix_range": [cfg.calib_vix_lo, cfg.calib_vix_hi],
        "trades_outside_pct": round(len(out) / len(trades) * 100, 1),
        "abs_pnl_outside_pct": round(sum(abs(t["net_rs"]) for t in out) / gross_abs * 100, 1),
        "net_rs_outside": round(sum(t["net_rs"] for t in out), 2),
        "net_rs_inside": round(sum(t["net_rs"] for t in trades
                                   if not t.get("extrapolated")), 2),
        "note": ("Trades whose entry VIX fell outside the range the pricing "
                 "model was calibrated on. Their premiums are extrapolated, "
                 "so their P&L is the least trustworthy part of the study."),
    }


def summarise(trades: list, cfg: StudyConfig) -> dict:
    if not trades:
        return {"n": 0}
    net_pct = [t["net_pct"] for t in trades]
    net_rs = [t["net_rs"] for t in trades]

    by_month = defaultdict(list)
    for t in trades:
        by_month[t["month"]].append(t)
    months = []
    for m in sorted(by_month):
        rows = by_month[m]
        rs = [r["net_rs"] for r in rows]
        pct = [r["net_pct"] for r in rows]
        months.append({
            "month": m, "trades": len(rows),
            "net_rs": round(sum(rs), 2),
            "mean_net_pct": round(statistics.mean(pct), 2),
            "win_rate_pct": round(sum(1 for p in pct if p > 0) / len(pct) * 100, 1),
            "best_rs": round(max(rs), 2), "worst_rs": round(min(rs), 2),
        })
    month_totals = [m["net_rs"] for m in months]

    # Layer 1 — the index alone, no option model anywhere in these numbers.
    signed = [t["signed_move_pts"] for t in trades]
    followed = sum(1 for s in signed if s > 0)
    underlying = {
        "n": len(signed),
        "continued_pct": round(followed / len(signed) * 100, 1),
        "mean_signed_pts": round(statistics.mean(signed), 2),
        "median_signed_pts": round(statistics.median(signed), 2),
        "stdev_pts": round(statistics.pstdev(signed), 2),
        "mean_signed_ci95": _bootstrap_ci(signed, cfg.bootstrap_iters),
        "note": ("Positive = the index moved the way the 15:00 signal pointed, "
                 "measured 15:05 to 09:50. Real index prints only."),
    }

    return {
        "n": len(trades),
        "from": trades[0]["date"],
        "to": trades[-1]["date"],
        "underlying": underlying,
        "option": {
            **_pct_stats(net_pct),
            "mean_gross_pct": round(statistics.mean(t["gross_pct"] for t in trades), 2),
            "mean_net_pct_ci95": _bootstrap_ci(net_pct, cfg.bootstrap_iters),
            "total_net_rs": round(sum(net_rs), 2),
            "mean_net_rs": round(statistics.mean(net_rs), 2),
            "total_charges_rs": round(sum(t["charges_rs"] for t in trades), 2),
            "mean_premium_rs": round(statistics.mean(t["fill_in"] * t["qty"]
                                                     for t in trades), 2),
            **_drawdown(net_rs),
        },
        "per_month": {
            "rows": months,
            "months": len(months),
            "mean_net_rs": round(statistics.mean(month_totals), 2),
            "median_net_rs": round(statistics.median(month_totals), 2),
            "positive_months": sum(1 for x in month_totals if x > 0),
            "mean_trades": round(statistics.mean(m["trades"] for m in months), 1),
        },
        "trust": _trust(trades, cfg),
        "by_direction": _group(trades, "direction"),
        "by_vix": _group(trades, "vix_bucket"),
        "by_dte": _group(trades, "dte_bucket"),
        "by_gap": _group(trades, "gap_bucket"),
        "lots": cfg.lots,
        "lot_size": cfg.lot_size,
        "qty": cfg.qty,
    }


def run_study(spine: pd.DataFrame, vix_df: pd.DataFrame, model: OptionModel,
              cfg: StudyConfig, start: Optional[date] = None) -> dict:
    days = build_days(spine)
    trades, skipped = build_trades(days, VixLookup(vix_df), model, cfg, start)
    out = summarise(trades, cfg)
    out["skipped"] = skipped
    out["trades"] = trades
    return out


# --- Robustness -----------------------------------------------------------
# Deliberately LAYER 1 ONLY. Sweeping entry/exit clocks through the option
# model would be searching a modelled surface for the best-looking cell, which
# is how a study talks itself into an edge. Index points cannot be tuned by a
# pricing assumption, so the sweep answers the only fair question: is 15:00 ->
# 09:50 special, or is the whole neighbourhood the same shape?

SWEEP_ENTRIES = [(15, 0), (15, 10), (15, 20)]
SWEEP_EXITS = [(9, 15), (9, 20), (9, 50), (10, 15), (11, 0)]


def variant_sweep(days: "dict[date, Day]", cfg: StudyConfig,
                  start: Optional[date] = None) -> list:
    ordered = sorted(days)
    out = []
    for e_hm in SWEEP_ENTRIES:
        for x_hm in SWEEP_EXITS:
            signed = []
            for i in range(1, len(ordered) - 1):
                d = ordered[i]
                if start and d < start:
                    continue
                day, prev_day, next_day = days[d], days[ordered[i - 1]], days[ordered[i + 1]]
                e_bar, x_bar = day.bar(e_hm), next_day.bar(x_hm)
                open_bar = day.bar(OPEN_BAR)
                pclose = close_ref(prev_day)
                if e_bar is None or x_bar is None or pclose is None:
                    continue
                # Score the SAME rule the headline runs — a sweep that always
                # read prev_close was stress-testing a different hypothesis
                # than the day_open card rendered above it (review catch).
                if cfg.signal_mode == SIGNAL_DAY_OPEN:
                    if open_bar is None:
                        continue
                    reference = open_bar[0]
                else:
                    reference = pclose
                gap = e_bar[0] - reference
                if abs(gap) <= cfg.deadband_pts:
                    continue
                signed.append((x_bar[0] - e_bar[3]) * (1 if gap > 0 else -1))
            if len(signed) < 30:
                continue
            wins = sum(1 for s in signed if s > 0)
            out.append({
                "entry": f"{e_hm[0]:02d}:{e_hm[1]:02d}",
                "exit": f"{x_hm[0]:02d}:{x_hm[1]:02d}",
                "n": len(signed),
                "continued_pct": round(wins / len(signed) * 100, 1),
                "mean_signed_pts": round(statistics.mean(signed), 2),
                "median_signed_pts": round(statistics.median(signed), 2),
            })
    return out
