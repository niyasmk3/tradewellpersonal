"""B09 dedicated study: after a VWAP ±kσ touch, does NIFTY return to VWAP?

Two layers, kept honestly apart:

  UNDERLYING layer — every qualifying touch episode over the FULL index
  history (~3y of 5-min bars): did price tag VWAP again before extending a
  further sl_ext·σ beyond the band (the sigma-stop proxy) or the square-off?
  Time/bars to VWAP, MFE toward it, MAE beyond it. Big sample; answers the
  headline probability. Same-bar ambiguity resolves STOP FIRST, and the
  outcome scan starts on the bar AFTER the touch (nothing fills inside the
  touch bar).

  OPTION layer — the same episodes inside the chain era only: real ATM
  premium at the first snapshot after the touch, premium max/min while the
  episode runs, premium at the outcome instant, net rupee P&L after full
  costs. Never modelled, never an index proxy.

Episode discipline matches the B09 strategy exactly: a touch disarms its
side until price returns to VWAP, so a band-walking afternoon is ONE
episode with one outcome (usually a stop), not twenty signals.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from app.lab import costs as costs_mod
from app.lab import data as data_mod
from app.lab import features as feat
from app.lab.engine import HistChainView, QUOTE_STALE_S
from app.lab.fills import fill_price, lot_size_for
from app.lab.models import BUY, LegSpec
from app.lab.fills import resolve_leg
from app.market.calendar import IST

log = logging.getLogger("tradewell.lab.vwap3")

RESEARCH_MULTS = (2.0, 2.5, 3.0, 3.5, 4.0)
_SESSION_OPEN_MIN = 555
MIN_OBS_RELIABLE = 100      # touch-level observations before headline trust


def _hour_bucket(minute: int) -> str:
    if minute < 600:
        return "09:15-10:00"
    if minute < 690:
        return "10:00-11:30"
    if minute < 810:
        return "11:30-13:30"
    if minute < 870:
        return "13:30-14:30"
    return "14:30-15:15"


def _touches_for_day(day_df: pd.DataFrame, mult: float, sl_ext: float,
                     avoid_first_min: int, squareoff_min: int) -> list:
    """Touch episodes + underlying outcomes for one session. day_df must be
    one IST session of featured bars, in order."""
    out = []
    armed = {"up": True, "dn": True}
    ordinal = {"up": 0, "dn": 0}
    rows = day_df.to_dict("records")
    for i, r in enumerate(rows):
        vwap, sd = r.get("vwap"), r.get("vwap_sd")
        if vwap is None or sd is None or not np.isfinite(vwap) or \
                not np.isfinite(sd) or sd <= 0:
            continue
        upper, lower = vwap + mult * sd, vwap - mult * sd
        if r["low"] <= vwap <= r["high"]:
            armed["up"] = armed["dn"] = True
        for side, hit, band in (("up", r["high"] >= upper, upper),
                                ("dn", r["low"] <= lower, lower)):
            if not hit or not armed[side]:
                continue
            armed[side] = False
            ordinal[side] += 1
            if int(r["minute"]) < _SESSION_OPEN_MIN + avoid_first_min or \
                    int(r["minute"]) >= squareoff_min:
                continue
            out.append(_scan_outcome(rows, i, side, ordinal[side], mult,
                                     sl_ext, squareoff_min, vwap, sd, band))
    return [t for t in out if t is not None]


def _scan_outcome(rows: list, i: int, side: str, ordinal: int, mult: float,
                  sl_ext: float, squareoff_min: int, vwap0: float,
                  sd0: float, band0: float) -> Optional[dict]:
    """Walk forward from the bar AFTER the touch. Bands/VWAP re-evaluate
    each bar (they move); stop level = current band + sl_ext·current σ.
    A bar that reaches both VWAP and the stop counts as STOP (conservative)."""
    r0 = rows[i]
    touch = {
        "ts": int(r0["ts"]), "minute": int(r0["minute"]),
        "day": datetime.fromtimestamp(int(r0["ts"]), IST).date().isoformat(),
        "side": "upper" if side == "up" else "lower",
        "ordinal": ordinal,
        "price": round(float(r0["close"]), 2),
        "vwap": round(vwap0, 2), "sd": round(sd0, 2),
        "band": round(band0, 2),
        "dist": round(abs(float(r0["close"]) - vwap0), 2),
        "width": round(2 * mult * sd0, 2),
        "hour": _hour_bucket(int(r0["minute"])),
        "dow": datetime.fromtimestamp(int(r0["ts"]), IST).strftime("%a"),
        "close_inside": bool(r0["close"] < band0) if side == "up"
                        else bool(r0["close"] > band0),
    }
    # VWAP slope bucket over the 12 bars before the touch
    j0 = max(0, i - 12)
    touch["slope"] = feat.vwap_slope_bucket(
        float(r0["vwap"]), float(rows[j0]["vwap"]), i - j0,
        float(r0.get("atr") or 0)) if i > j0 else feat.SLOPE_FLAT

    mfe = 0.0      # best progress toward vwap, points
    mae = 0.0      # worst extension beyond band, points
    outcome, t_out, bars_out = "open_at_squareoff", None, None
    for k in range(i + 1, len(rows)):
        r = rows[k]
        vwap, sd = float(r["vwap"]), float(r["vwap_sd"])
        if not np.isfinite(sd) or sd <= 0:
            continue
        band = vwap + mult * sd if side == "up" else vwap - mult * sd
        stop = band + sl_ext * sd if side == "up" else band - sl_ext * sd
        if side == "up":
            mfe = max(mfe, float(r0["close"]) - float(r["low"]))
            mae = max(mae, float(r["high"]) - band0)
            hit_stop = r["high"] >= stop
            hit_vwap = r["low"] <= vwap
        else:
            mfe = max(mfe, float(r["high"]) - float(r0["close"]))
            mae = max(mae, band0 - float(r["low"]))
            hit_stop = r["low"] <= stop
            hit_vwap = r["high"] >= vwap
        if int(r["minute"]) >= squareoff_min:
            outcome = "open_at_squareoff"
            t_out, bars_out = int(r["ts"]), k - i
            break
        if hit_stop:                      # conservative: stop first
            outcome = "stop_first"
            t_out, bars_out = int(r["ts"]), k - i
            break
        if hit_vwap:
            outcome = "vwap_first"
            t_out, bars_out = int(r["ts"]), k - i
            break
    touch.update({
        "outcome": outcome,
        "returned": outcome == "vwap_first",
        "minutes_to_out": ((t_out - touch["ts"]) // 60) if t_out else None,
        "bars_to_out": bars_out,
        "mfe_pts": round(mfe, 2),
        "mae_pts": round(mae, 2),
        "mae_sigma": round(mae / sd0, 2) if sd0 > 0 else None,
        "out_ts": t_out,
    })
    return touch


def _mode_qualifies(rows_by_ts: dict, touch: dict, mode: str,
                    confirm_window: int = 3) -> bool:
    """Would entry mode B / C(reversal) have triggered for this touch?
    (A always qualifies.) Used for the ENTRY MODE × SUCCESS heatmap."""
    if mode == "A":
        return True
    if mode == "B":
        return bool(touch["close_inside"])
    day_rows = rows_by_ts.get(touch["day"], [])
    idx = next((k for k, r in enumerate(day_rows)
                if int(r["ts"]) == touch["ts"]), None)
    if idx is None:
        return False
    for r in day_rows[idx + 1: idx + 1 + confirm_window]:
        if touch["side"] == "upper" and r["close"] < r["open"]:
            return True
        if touch["side"] == "lower" and r["close"] > r["open"]:
            return True
    return False


def _option_leg(touch: dict, reader, spot_fn) -> Optional[dict]:
    """Real ATM premium path for one touch inside the chain era."""
    snap0 = reader.snap_after(touch["ts"] + 1, 900)
    if snap0 is None:
        return None
    right = "PE" if touch["side"] == "upper" else "CE"
    view = HistChainView(reader, snap0, spot_fn(snap0))
    q, why = resolve_leg(view, LegSpec(side=BUY, right=right), 0, 99.0)
    if q is None:
        return None
    entry = fill_price(q, BUY, 0.25)
    lot = lot_size_for(touch["day"])
    end_ts = touch["out_ts"] or (touch["ts"] + 6 * 3600)
    hi = lo = q.ltp
    exit_px = None
    approx = False
    last_qq = None
    import bisect
    lst = reader.snap_ts_list()
    i0 = bisect.bisect_right(lst, snap0)
    for snap in lst[i0:]:
        if snap > end_ts + 900:
            break
        v = HistChainView(reader, snap, spot_fn(snap))
        qq = v.quote(q.expiry, q.strike, right)
        if qq is None or qq.ltp <= 0:
            continue
        hi, lo = max(hi, qq.ltp), min(lo, qq.ltp)
        if snap >= end_ts:
            exit_px = fill_price(qq, "SELL", 0.25)
            break
        last_qq = qq
    if exit_px is None and last_qq is not None and \
            end_ts - last_qq.ts <= 900:
        # outcome landed after the day's last snapshot (typically the
        # square-off): value the exit at the last quote and SAY SO
        exit_px = fill_price(last_qq, "SELL", 0.25)
        approx = True
    if exit_px is None:
        return None
    cost = costs_mod.order_cost("OPT", "BUY", entry, lot, touch["day"]) + \
        costs_mod.order_cost("OPT", "SELL", exit_px, lot, touch["day"])
    return {
        "symbol": "NIFTY %s %s %s" % (q.expiry, int(q.strike), right),
        "right": right, "entry": entry, "exit": exit_px,
        "prem_max": round(hi, 2), "prem_min": round(lo, 2),
        "gross": round((exit_px - entry) * lot, 2),
        "costs": round(cost, 2),
        "net": round((exit_px - entry) * lot - cost, 2),
        "exit_approx": approx,
    }


def _rate_bucket(touches: list, key_fn) -> dict:
    out: dict = {}
    for t in touches:
        k = str(key_fn(t))
        b = out.setdefault(k, {"n": 0, "returned": 0})
        b["n"] += 1
        b["returned"] += 1 if t["returned"] else 0
    for b in out.values():
        b["rate"] = round(b["returned"] / b["n"] * 100, 1) if b["n"] else None
        b["low_sample"] = b["n"] < 30
    return out


def _summary(touches: list) -> dict:
    n = len(touches)
    if n == 0:
        return {"n": 0}
    upper = [t for t in touches if t["side"] == "upper"]
    lower = [t for t in touches if t["side"] == "lower"]
    ret = [t for t in touches if t["returned"]]
    times = sorted(t["minutes_to_out"] for t in ret
                   if t["minutes_to_out"] is not None)
    maes = sorted(t["mae_sigma"] for t in touches
                  if t["mae_sigma"] is not None)

    def _rate(sub):
        return round(sum(1 for t in sub if t["returned"]) / len(sub) * 100,
                     1) if sub else None

    def _med(v):
        return v[len(v) // 2] if v else None

    stops = sum(1 for t in touches if t["outcome"] == "stop_first")
    return {
        "n": n,
        "upper_n": len(upper), "lower_n": len(lower),
        "return_rate": _rate(touches),
        "return_rate_upper": _rate(upper),
        "return_rate_lower": _rate(lower),
        "median_minutes_to_vwap": _med(times),
        "avg_minutes_to_vwap": round(sum(times) / len(times), 1)
                               if times else None,
        "median_mae_sigma": _med(maes),
        "avg_mae_sigma": round(sum(maes) / len(maes), 2) if maes else None,
        "pct_vwap_before_stop": _rate(touches),
        "pct_stop_before_vwap": round(stops / n * 100, 1),
        "pct_open_at_squareoff": round(
            sum(1 for t in touches
                if t["outcome"] == "open_at_squareoff") / n * 100, 1),
        "reliable": n >= MIN_OBS_RELIABLE,
        "reliability_note": None if n >= MIN_OBS_RELIABLE else
            "only %d observations (<%d) — treat as indicative, not "
            "established" % (n, MIN_OBS_RELIABLE),
    }


def run_study(sigma_mult: float = 3.0, timeframe: str = "5m",
              sl_ext: float = 0.50, avoid_first_min: int = 15,
              squareoff_min: int = 915,
              research_mults: tuple = RESEARCH_MULTS) -> dict:
    df = data_mod.load_index_frame()
    if df.empty or len(df) < 1000:
        raise ValueError("index history missing — sync the patterns store")
    if timeframe != "5m":
        df = data_mod.resample(df, timeframe)
    df = feat.add_features(df)
    vix_df = data_mod.load_vix_daily()

    by_day: dict = {}
    for day_key, day_df in df.groupby("day"):
        by_day[day_key] = day_df.reset_index(drop=True)

    def _collect(mult: float) -> list:
        out = []
        for day_key in sorted(by_day):
            out.extend(_touches_for_day(by_day[day_key], mult, sl_ext,
                                        avoid_first_min, squareoff_min))
        return out

    touches = _collect(sigma_mult)

    # per-day context: VIX percentile regime, band-width quintile
    vixp_by_day: dict = {}
    for t in touches:
        d = t["day"]
        if d not in vixp_by_day:
            vixp_by_day[d] = data_mod.vix_percentile(vix_df, t["ts"])
        p = vixp_by_day[d]
        t["vol_regime"] = ("unknown" if p is None else
                          "low" if p < 25 else "normal" if p < 75 else
                          "high" if p < 90 else "extreme")
    widths = sorted(t["width"] / t["vwap"] for t in touches if t["vwap"])
    if widths:
        qs = [widths[int(len(widths) * q)] for q in (0.2, 0.4, 0.6, 0.8)]
        for t in touches:
            w = t["width"] / t["vwap"]
            t["width_q"] = "Q%d" % (1 + sum(1 for q in qs if w > q))

    rows_by_day: dict = {}
    for k, day_df in by_day.items():
        iso = datetime.fromtimestamp(int(day_df.iloc[0]["ts"]),
                                     IST).date().isoformat()
        rows_by_day[iso] = day_df.to_dict("records")

    # ENTRY MODE heatmap: rate among touches where the mode qualifies
    mode_rates = {}
    for m in ("A", "B", "C"):
        subset = [t for t in touches
                  if _mode_qualifies(rows_by_day, t, m)]
        mode_rates[m] = {
            "n": len(subset),
            "rate": round(sum(1 for t in subset if t["returned"])
                          / len(subset) * 100, 1) if subset else None,
            "low_sample": len(subset) < 30,
        }

    # option layer (chain era, requested mult only)
    reader = data_mod.chain_reader
    era = reader.span()
    opt_pe = opt_ce = 0.0
    opt_pe_n = opt_ce_n = opt_pe_w = opt_ce_w = 0
    opt_rows = 0
    equity, peak, dd = 0.0, 0.0, 0.0
    if era:
        ts_arr = df["ts"].values

        def spot_fn(snap_ts: int) -> float:
            import bisect
            j = max(0, bisect.bisect_right(ts_arr, snap_ts) - 1)
            return float(df.iloc[j]["close"])
        for t in touches:
            if t["ts"] < era[0]:
                continue
            leg = _option_leg(t, reader, spot_fn)
            t["option"] = leg
            if leg is None:
                continue
            opt_rows += 1
            equity += leg["net"]
            peak = max(peak, equity)
            dd = min(dd, equity - peak)
            if leg["right"] == "PE":
                opt_pe += leg["net"]
                opt_pe_n += 1
                opt_pe_w += 1 if leg["net"] > 0 else 0
            else:
                opt_ce += leg["net"]
                opt_ce_n += 1
                opt_ce_w += 1 if leg["net"] > 0 else 0
    gross_p = sum((t.get("option") or {}).get("net", 0) for t in touches
                  if (t.get("option") or {}).get("net", 0) > 0)
    gross_l = -sum((t.get("option") or {}).get("net", 0) for t in touches
                   if (t.get("option") or {}).get("net", 0) < 0)

    result = {
        "official_mult": sigma_mult,
        "timeframe": timeframe,
        "sl_ext": sl_ext,
        "span": {"from": touches[0]["day"] if touches else None,
                 "to": touches[-1]["day"] if touches else None},
        "summary": _summary(touches),
        "heatmaps": {
            "hour": _rate_bucket(touches, lambda t: t["hour"]),
            "dow": _rate_bucket(touches, lambda t: t["dow"]),
            "slope": _rate_bucket(touches, lambda t: t["slope"]),
            "vol_regime": _rate_bucket(touches, lambda t: t["vol_regime"]),
            "width_q": _rate_bucket(touches, lambda t: t.get("width_q",
                                                             "?")),
            "ordinal": _rate_bucket(
                touches, lambda t: str(t["ordinal"]) if t["ordinal"] < 3
                else "3+"),
            "entry_mode": mode_rates,
        },
        "research_mults": {
            str(m): _summary(_collect(m)) for m in research_mults
            if m != sigma_mult
        },
        "option_layer": {
            "n": opt_rows,
            "era": {"from": data_mod._fmt_day(era[0]) if era else None,
                    "to": data_mod._fmt_day(era[1]) if era else None},
            "pe": {"n": opt_pe_n, "net": round(opt_pe, 2),
                   "win_rate": round(opt_pe_w / opt_pe_n * 100, 1)
                               if opt_pe_n else None},
            "ce": {"n": opt_ce_n, "net": round(opt_ce, 2),
                   "win_rate": round(opt_ce_w / opt_ce_n * 100, 1)
                               if opt_ce_n else None},
            "net": round(opt_pe + opt_ce, 2),
            "expectancy": round((opt_pe + opt_ce) / opt_rows, 2)
                          if opt_rows else None,
            "profit_factor": round(gross_p / gross_l, 2)
                             if gross_l > 0 else None,
            "max_drawdown": round(dd, 2),
            "note": "REAL premiums from the chain archive; exits valued at "
                    "the outcome instant (VWAP tag / stop / square-off), "
                    "full costs. Not comparable to backtest runs that carry "
                    "different exits.",
        },
        "touches_recent": [
            {k: v for k, v in t.items() if k != "out_ts"}
            for t in touches[-250:]
        ],
    }
    return result


def run_sweeps(sigma_mult: float = 3.0) -> dict:
    """Engine-based research grids over the chain era, run directly (no
    version/run rows — these are probes; the official v1 record stays the
    Backtest Lab's). Each cell: trades, win rate, expectancy, net."""
    from app.lab import catalog
    from app.lab.engine import Backtester
    from app.lab.models import validate_params
    sdef = catalog.get("B09")
    era = data_mod.chain_reader.span()
    if not era:
        return {}
    frm, to = era[0], era[1]

    def _cell(over: dict) -> dict:
        p = validate_params(sdef.params, dict(over, sigma_mult=sigma_mult))
        bt = Backtester(sdef, p, p.get("timeframe") or "5m", frm, to, 500000)
        r = bt.run()
        if r.status != "done":
            return {"status": r.status}
        n = len(r.trades)
        net = sum(t["pnl_net"] for t in r.trades)
        wins = sum(1 for t in r.trades if t["pnl_net"] > 0)
        return {"n": n, "net": round(net, 0),
                "win_rate": round(wins / n * 100, 1) if n else None,
                "expectancy": round(net / n, 0) if n else None,
                "low_sample": n < 30}

    modes = {"touch": "A", "close_inside": "B", "rejection": "C"}
    grid = {}
    for mode, letter in modes.items():
        for ext in (0.25, 0.50, 0.75, 1.00):
            grid["%s|%.2f" % (letter, ext)] = _cell(
                {"entry_mode": mode, "sl_sigma_ext": ext})
    strikes = {}
    for off in (-2, -1, 0, 1, 2):
        strikes[str(off)] = _cell({"strike_offset": off})
    strikes["delta"] = _cell({"strike_sel": "delta"})
    expiry = {r: _cell({"expiry_rule": r}) for r in ("weekly", "monthly")}
    targets = {t: _cell({"target_mode": t})
               for t in ("vwap", "premium_pct", "risk_reward",
                         "trail_after")}
    return {"entry_mode_x_sl_ext": grid, "strike": strikes,
            "expiry": expiry, "target_mode": targets,
            "note": "engine probes over the chain era only (%s..%s); "
                    "n < 30 cells are anecdotes"
                    % (data_mod._fmt_day(frm), data_mod._fmt_day(to))}


def day_detail(day: str, sigma_mult: float = 3.0,
               timeframe: str = "5m") -> dict:
    """One session's candles + VWAP + bands + touch markers, for the trade
    chart. B09 trades that day (latest runs) ride along as markers."""
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
    lo = int(d0.replace(hour=9, minute=0).timestamp())
    hi = int(d0.replace(hour=15, minute=35).timestamp())
    df = data_mod.load_index_frame(lo - 40 * 86400, hi)
    if timeframe != "5m":
        df = data_mod.resample(df, timeframe)
    df = feat.add_features(df)
    day_df = df[(df["ts"] >= lo) & (df["ts"] <= hi)].reset_index(drop=True)
    if day_df.empty:
        raise ValueError("no bars for %s" % day)
    m = float(sigma_mult)
    candles, vwap_line, up_line, dn_line = [], [], [], []
    for _, r in day_df.iterrows():
        ts = int(r["ts"])
        candles.append({"ts": ts, "open": r["open"], "high": r["high"],
                        "low": r["low"], "close": r["close"]})
        if np.isfinite(r["vwap"]) and np.isfinite(r["vwap_sd"]):
            vwap_line.append({"ts": ts, "v": round(float(r["vwap"]), 2)})
            up_line.append({"ts": ts, "v": round(
                float(r["vwap"]) + m * float(r["vwap_sd"]), 2)})
            dn_line.append({"ts": ts, "v": round(
                float(r["vwap"]) - m * float(r["vwap_sd"]), 2)})
    touches = _touches_for_day(day_df, m, 0.5, 0, 930)

    from app.lab.store import lab_store
    trades = []
    for run in lab_store.runs("B09", limit=5):
        if run["status"] != "done":
            continue
        for t in lab_store.run_trades(run["run_id"], 1000):
            if datetime.fromtimestamp(t["entry_ts"], IST).date().isoformat() \
                    == day:
                trades.append({
                    "run_id": run["run_id"], "tag": t["tag"],
                    "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
                    "exit_reason": t["exit_reason"],
                    "pnl_net": t["pnl_net"],
                    "legs": t["legs"],
                })
        if trades:
            break
    return {"day": day, "sigma_mult": m, "timeframe": timeframe,
            "candles": candles, "vwap": vwap_line, "upper": up_line,
            "lower": dn_line,
            "touches": [{k: v for k, v in t.items() if k != "out_ts"}
                        for t in touches],
            "trades": trades}
