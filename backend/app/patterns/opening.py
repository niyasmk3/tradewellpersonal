"""Opening-window study (09:15-10:00 IST): is the open loud, and is it legible?

The hypothesis under test: the first 45 minutes are the most volatile stretch
of the session and carry more opportunity. This module measures BOTH halves of
that claim on the 3-year 5-min spine — loudness is a fact to quantify,
opportunity is a prediction to grade — under the lab's standing discipline:
every number is a historical frequency with its n attached, cells under
MIN_CELL vanish, nothing under MIN_TENDENCY may be called a tendency, and a
45-55% hit rate prints as "coin" no matter how big the sample.

Feature discipline (no lookahead): every conditioning feature is computable at
10:00 sharp — the prior session's daily bar plus the nine 5-min bars opening
09:15-09:55 (the ninth closes at 10:00). Outcomes are scored strictly on later
bars of the same session. Rate cells (trend-day, gap-fill) are judged against
their BASE rate, not against 50% — "34% of gap-up days trend" is only a
tendency if the unconditional trend rate is meaningfully different.

Known limits, stated rather than fudged:
- "VWAP" is proxy-VWAP: NIFTYBEES volume weighting NIFTY typical price.
- OR45-size terciles are full-sample boundaries (descriptive research); the
  stored boundaries are what a live read must consume, never a daily recompute.
- No expiry-day flag: NIFTY's weekly-expiry weekday moved during this window
  and a guessed calendar would silently poison every cell it conditions.
  Weekday cells carry that regime signal crudely until a real expiry calendar
  lands.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.patterns.analysis import WEEKDAYS, _with_ist
from app.patterns.tendencies import MIN_CELL, MIN_TENDENCY, _cell

FIRST45_BARS = 9          # bars opening 09:15..09:55; the ninth closes 10:00
OR15_BARS = 3
OR30_BARS = 6
FULL_SESSION_MIN_BARS = 70

GAP_FLAT_BPS = 10.0       # |gap| <= this is "flat" — inside NIFTY's usual noise
GAP_BIG_BPS = 45.0        # |gap| > this is a "big" gap
GAP_BUCKETS = ["big_down", "down", "flat", "up", "big_up"]

PACE_SESSIONS = 60        # trailing sessions for the first-45 volume yardstick
PACE_MIN_SESSIONS = 20
PACE_HIGH = 1.25
PACE_LOW = 0.8

RATE_EDGE_PP = 5.0        # a rate within 5pp of its base is "≈ base rate"

FWD30_BARS = 6            # 30/60-min outcome horizons after an OR30 break
FWD60_BARS = 12
BARS_BY_11 = 21           # bar index 20 closes 11:00; fill_idx < 21 == by 11:00


def _bps(x) -> float:
    return round(float(x) * 10000, 1)


def _rate_cell(flags: pd.Series, base: float) -> dict:
    """A conditional RATE judged against its unconditional base — the coin
    band is meaningless for events whose base rate is nowhere near 50%."""
    n = int(len(flags))
    rate = float(flags.mean()) if n else 0.0
    if n < MIN_TENDENCY:
        verdict = "sample too small"
    elif abs(rate - base) < RATE_EDGE_PP / 100:
        verdict = "= base rate"
    else:
        verdict = "tendency"
    return {"n": n, "rate": round(rate, 3), "base_rate": round(base, 3),
            "edge_pp": round(100 * (rate - base), 1), "verdict": verdict}


def _gap_bucket(gap_bps: float) -> str:
    if gap_bps > GAP_BIG_BPS:
        return "big_up"
    if gap_bps > GAP_FLAT_BPS:
        return "up"
    if gap_bps < -GAP_BIG_BPS:
        return "big_down"
    if gap_bps < -GAP_FLAT_BPS:
        return "down"
    return "flat"


def build_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """One row per full session: 10:00-visible features + rest-of-day outcomes.

    Only sessions that open exactly 09:15 with all nine first-45 bars present
    and >= FULL_SESSION_MIN_BARS total qualify — a disrupted morning would
    register as a fake tiny opening range. The prior close comes from ALL
    stored sessions, so a dropped partial day does not make the next gap span
    two days.
    """
    d = _with_ist(df).sort_values("ts")
    all_days = d.groupby("date", sort=True)
    prev_close = all_days["close"].last().shift(1)
    prev_high = all_days["high"].max().shift(1)
    prev_low = all_days["low"].min().shift(1)

    rows = []
    for date_s, g in d.groupby("date", sort=True):
        g = g.reset_index(drop=True)
        if len(g) < FULL_SESSION_MIN_BARS or g["hhmm"].iloc[0] != "09:15":
            continue
        f45 = g.iloc[:FIRST45_BARS]
        if f45["hhmm"].iloc[-1] != "09:55":
            continue                      # a hole inside the window disqualifies

        o = float(g["open"].iloc[0])
        day_high, day_low = float(g["high"].max()), float(g["low"].min())
        close = float(g["close"].iloc[-1])
        c0955 = float(f45["close"].iloc[-1])
        pc = prev_close.get(date_s)
        pc = float(pc) if pd.notna(pc) else None
        gap = (o - pc) / pc if pc else None

        # Proxy-VWAP over the window; None on a dead proxy, never a fake 0.
        vsum = float(f45["vol_proxy"].sum())
        vwap = None
        if vsum > 0:
            tp = (f45["high"] + f45["low"] + f45["close"]) / 3
            vwap = float((tp * f45["vol_proxy"]).sum() / vsum)

        # First close outside OR30, scanning bars after the range completes.
        or30_h = float(g["high"].iloc[:OR30_BARS].max())
        or30_l = float(g["low"].iloc[:OR30_BARS].min())
        brk_idx = brk_dir = None
        for i in range(OR30_BARS, len(g)):
            c = float(g["close"].iloc[i])
            if c > or30_h:
                brk_idx, brk_dir = i, 1
                break
            if c < or30_l:
                brk_idx, brk_dir = i, -1
                break
        brk_fwd30 = brk_fwd60 = None
        if brk_idx is not None:
            bc = float(g["close"].iloc[brk_idx])
            if brk_idx + FWD30_BARS < len(g):
                brk_fwd30 = float(g["close"].iloc[brk_idx + FWD30_BARS]) / bc - 1
            if brk_idx + FWD60_BARS < len(g):
                brk_fwd60 = float(g["close"].iloc[brk_idx + FWD60_BARS]) / bc - 1

        # First touch of the prior close (directional: a gap-up fills downward).
        fill_idx = None
        if pc is not None and gap is not None and abs(gap) * 1e4 > GAP_FLAT_BPS:
            lows, highs = g["low"].to_numpy(), g["high"].to_numpy()
            for i in range(len(g)):
                if (gap > 0 and lows[i] <= pc) or (gap < 0 and highs[i] >= pc):
                    fill_idx = i
                    break

        span = max(day_high - day_low, 1e-9)
        pos = (close - day_low) / span
        f45_range = (float(f45["high"].max()) - float(f45["low"].min()))
        rest = g.iloc[FIRST45_BARS:]
        bar_rng_45 = float(((f45["high"] - f45["low"]) / o).mean())
        bar_rng_rest = float(((rest["high"] - rest["low"]) / o).mean())

        ph, pl = prev_high.get(date_s), prev_low.get(date_s)
        prev_pos = None
        if pc is not None and pd.notna(ph) and pd.notna(pl) and ph > pl:
            prev_pos = min(max((pc - float(pl)) / (float(ph) - float(pl)), 0.0), 1.0)

        rows.append({
            "date": date_s,
            "weekday": int(g["weekday"].iloc[0]),
            "open": o, "close": close, "c0955": c0955,
            "gap_bps": _bps(gap) if gap is not None else None,
            "gap_bucket": _gap_bucket(_bps(gap)) if gap is not None else None,
            "prev_pos": prev_pos,
            "or15_bps": _bps((float(g["high"].iloc[:OR15_BARS].max())
                              - float(g["low"].iloc[:OR15_BARS].min())) / o),
            "or30_bps": _bps((or30_h - or30_l) / o),
            "or45_bps": _bps(f45_range / o),
            "or45_day_share": round(f45_range / span, 3),
            "f45_ret": c0955 / o - 1,
            "f45_dir": int(np.sign(c0955 - o)),
            "above_vwap": bool(c0955 >= vwap) if vwap is not None else None,
            "f45_vol": vsum,
            "bar_range_ratio": (round(bar_rng_45 / bar_rng_rest, 2)
                                if bar_rng_rest > 0 else None),
            "brk_dir": brk_dir,
            "brk_idx": brk_idx,
            "brk_fwd30": brk_fwd30,
            "brk_fwd60": brk_fwd60,
            "fill_idx": fill_idx,
            "drift": close / c0955 - 1,   # 10:00 -> close, the tradeable rest
            "trend_day": bool(pos >= 0.75 or pos <= 0.25),
            "gap_faded": (bool(close < o) if (gap or 0) > 0 else bool(close > o))
                         if gap is not None else None,
        })

    sess = pd.DataFrame(rows)
    if sess.empty:
        return sess
    # First-45 volume pace vs the trailing median of PRIOR sessions only —
    # today's own volume must not sit inside its own yardstick, and neither
    # may dead-proxy sessions (volume_pace_curve's own rule: an absent proxy
    # is not a dead tape — a half-dead window would halve the median and
    # mislabel every recovery session "high").
    vol_hist = sess["f45_vol"].where(sess["f45_vol"] > 0)
    med = vol_hist.shift(1).rolling(PACE_SESSIONS, min_periods=PACE_MIN_SESSIONS).median()
    sess["pace"] = np.where((med > 0) & (sess["f45_vol"] > 0), sess["f45_vol"] / med, np.nan)
    sess["pace_bucket"] = pd.Series(
        np.select([sess["pace"] >= PACE_HIGH, sess["pace"] <= PACE_LOW],
                  ["high", "low"], default="normal"),
        index=sess.index).where(sess["pace"].notna(), None)
    return sess


def _volatility_profile(df: pd.DataFrame, sess: pd.DataFrame) -> dict:
    """The loudness half of the claim, per 5-min bar-of-day, full sessions only.

    Volume shares come from proxy-POSITIVE sessions only — an absent proxy is
    not a dead tape (tendencies.py's rule), and counting outage days as zero
    volume would dilute every share toward nothing.
    """
    d = _with_ist(df)
    d = d[d["date"].isin(set(sess["date"]))]
    d = d.assign(rng=(d["high"] - d["low"]) / d["open"])
    day_vol = d.groupby("date")["vol_proxy"].sum()
    dv = d[d["date"].isin(set(day_vol[day_vol > 0].index))]
    dv = dv.assign(vshare=dv["vol_proxy"] / dv["date"].map(day_vol))
    vshare_by_bar = dv.groupby("hhmm")["vshare"].mean()
    prof = [
        {"hhmm": hhmm, "n": int(len(g)),
         "median_range_bps": _bps(g["rng"].median()),
         "vol_share": (round(float(vshare_by_bar[hhmm]), 4)
                       if hhmm in vshare_by_bar.index else None)}
        for hhmm, g in d.groupby("hhmm", sort=True)
    ]
    ratio = sess["bar_range_ratio"].dropna()
    f45_share = dv[dv["hhmm"] < "10:00"].groupby("date")["vshare"].sum()
    rng_cell = {"n": int(len(ratio))}
    if len(ratio) >= MIN_CELL:
        rng_cell["median_ratio"] = round(float(ratio.median()), 2)
        rng_cell["sessions_louder_than_rest"] = round(float((ratio > 1).mean()), 3)
    else:
        rng_cell["note"] = f"under n={MIN_CELL} — no stats"
    return {
        "per_bar": prof,
        "first45_vs_rest_bar_range": rng_cell,
        "or45_share_of_day_range": {
            "median": round(float(sess["or45_day_share"].median()), 3),
            "note": "How much of the day's eventual range already printed by 10:00.",
        },
        "first45_vol_share_median": (round(float(f45_share.median()), 4)
                                     if len(f45_share) >= MIN_CELL else None),
    }


def _continuation_cells(sess: pd.DataFrame, or45_q: tuple) -> dict:
    """Does the first-45 direction carry through 10:00 -> close?  Signed
    drift, judged on the standard coin band via _cell."""
    s = sess[sess["f45_dir"] != 0].copy()
    signed = s["drift"] * s["f45_dir"]
    if len(signed) < MIN_CELL:
        return {"note": f"only {len(signed)} sessions with a nonzero "
                        "first-45 direction — no cell may form"}
    out = {"all": _cell(signed)}

    by = {}
    for b in GAP_BUCKETS:
        sel = s["gap_bucket"] == b
        if int(sel.sum()) >= MIN_CELL:
            by[b] = _cell(signed[sel])
    if by:
        out["by_gap"] = by

    lo, hi = or45_q
    tiers = {"small": s["or45_bps"] <= lo, "mid": (s["or45_bps"] > lo) & (s["or45_bps"] < hi),
             "large": s["or45_bps"] >= hi}
    out["by_or45_size"] = {k: _cell(signed[m]) for k, m in tiers.items()
                           if int(m.sum()) >= MIN_CELL}

    agree = s["above_vwap"].map({True: 1, False: -1})
    conf = s[agree.notna() & (agree == s["f45_dir"])]
    if len(conf) >= MIN_CELL:
        out["vwap_confirms"] = _cell((conf["drift"] * conf["f45_dir"]))
    div = s[agree.notna() & (agree != s["f45_dir"])]
    if len(div) >= MIN_CELL:
        out["vwap_diverges"] = _cell((div["drift"] * div["f45_dir"]))

    pace = {}
    for b in ("high", "normal", "low"):
        sel = s["pace_bucket"] == b
        if int(sel.sum()) >= MIN_CELL:
            pace[b] = _cell(signed[sel])
    if pace:
        out["by_pace"] = pace

    wd = {}
    for i, name in enumerate(WEEKDAYS):
        sel = s["weekday"] == i
        if int(sel.sum()) >= MIN_CELL:
            wd[name] = _cell(signed[sel])
    if wd:
        out["by_weekday"] = wd
    return out


def _trend_day_cells(sess: pd.DataFrame, or45_q: tuple) -> dict:
    base = float(sess["trend_day"].mean())
    out = {"base": {"n": int(len(sess)), "rate": round(base, 3)}}
    lo, hi = or45_q
    tiers = {"small": sess["or45_bps"] <= lo,
             "mid": (sess["or45_bps"] > lo) & (sess["or45_bps"] < hi),
             "large": sess["or45_bps"] >= hi}
    out["by_or45_size"] = {k: _rate_cell(sess.loc[m, "trend_day"], base)
                           for k, m in tiers.items() if int(m.sum()) >= MIN_CELL}
    by = {b: _rate_cell(sess.loc[sess["gap_bucket"] == b, "trend_day"], base)
          for b in GAP_BUCKETS if int((sess["gap_bucket"] == b).sum()) >= MIN_CELL}
    if by:
        out["by_gap"] = by
    pace = {b: _rate_cell(sess.loc[sess["pace_bucket"] == b, "trend_day"], base)
            for b in ("high", "normal", "low")
            if int((sess["pace_bucket"] == b).sum()) >= MIN_CELL}
    if pace:
        out["by_pace"] = pace
    return out


def _gap_cells(sess: pd.DataFrame) -> dict:
    s = sess[sess["gap_bucket"].notna() & (sess["gap_bucket"] != "flat")]
    fill_base = float(s["fill_idx"].notna().mean()) if len(s) else 0.0
    out = {"buckets_bps": {"flat": GAP_FLAT_BPS, "big": GAP_BIG_BPS},
           "fill_base_rate": round(fill_base, 3)}
    per = {}
    for b in GAP_BUCKETS:
        g = s[s["gap_bucket"] == b]
        if len(g) < MIN_CELL:
            continue
        filled = g["fill_idx"].notna()
        per[b] = {
            "n": int(len(g)),
            "fade_rate": round(float(g["gap_faded"].mean()), 3),
            "fill_by_close": _rate_cell(filled, fill_base),
            "fill_by_10": round(float((g["fill_idx"] < FIRST45_BARS).mean()), 3),
            "fill_by_11": round(float((g["fill_idx"] < BARS_BY_11).mean()), 3),
        }
    out["by_bucket"] = per
    return out


def _or30_breakout_cells(sess: pd.DataFrame) -> dict:
    b = sess[sess["brk_dir"].notna()]
    out = {
        "sessions_with_break": int(len(b)),
        "break_rate": round(float(len(b) / max(len(sess), 1)), 3),
        "break_up_share": round(float((b["brk_dir"] > 0).mean()), 3) if len(b) else None,
        # The break is OBSERVABLE at the breaking bar's CLOSE, (idx+1)*5
        # minutes after open — the bar's open predates the signal.
        "median_break_minutes_after_open": (round(float((b["brk_idx"].median() + 1) * 5), 1)
                                            if len(b) else None),
    }
    f30 = b[b["brk_fwd30"].notna()]
    if len(f30) >= MIN_CELL:
        out["fwd_30m"] = _cell(f30["brk_fwd30"] * f30["brk_dir"])
    f60 = b[b["brk_fwd60"].notna()]
    if len(f60) >= MIN_CELL:
        out["fwd_60m"] = _cell(f60["brk_fwd60"] * f60["brk_dir"])
    by = {}
    for bk in GAP_BUCKETS:
        sel = f30[f30["gap_bucket"] == bk]
        if len(sel) >= MIN_CELL:
            by[bk] = _cell(sel["brk_fwd30"] * sel["brk_dir"])
    if by:
        out["fwd_30m_by_gap"] = by
    return out


# ---- the fade backtest (Phase-3 gate) ---------------------------------------

def _bt_cell(pnls: list) -> dict:
    """Nearest-rank stats (upper median — a deliberate, documented divergence
    from _cell's interpolated median). Under MIN_CELL only n is emitted, per
    the module's own vanishing-cell rule."""
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    if n < MIN_CELL:
        return {"n": n, "note": f"under n={MIN_CELL} — stats suppressed"}
    s = sorted(pnls)
    return {
        "n": n,
        "win_rate": round(sum(1 for p in pnls if p > 0) / n, 3),
        "avg_bps": round(sum(pnls) / n, 1),
        "median_bps": round(s[n // 2], 1),
        "total_bps": round(sum(pnls), 1),
        "worst_bps": round(s[0], 1),
        "best_bps": round(s[-1], 1),
    }


def fade_backtest(df: pd.DataFrame, sess: "pd.DataFrame | None" = None) -> dict:
    """Phase-3 gate for the opening-fade idea: on a big-gap-up morning, enter
    at the 10:00 freeze AGAINST the first-45 direction; exit at close (the
    study cell's exact definition) and, separately, with a stop at the OR45
    extreme — a real trade needs an invalidation and the stop reshapes the
    distribution by cutting the tail where continuation rips.

    Index bps, no charges, entry at the 09:55 bar's close print — the shadow
    ledger is the net-of-charges test; this only decides whether the idea
    earns that slot. Stops are graded conservatively: a bar that OPENS beyond
    the stop exits at its open (gap-through), else at the stop level; a bar
    where both stop and close could apply counts as stopped.
    """
    if sess is None:
        sess = build_sessions(df)
    if sess.empty:
        return {"note": "No qualifying sessions."}
    # Same defensive sort as build_sessions — two consumers of one df must not
    # hold different ordering assumptions (review catch).
    d = _with_ist(df).sort_values("ts")
    bars_by_date = dict(tuple(d.groupby("date")))

    def _run(bucket: str):
        raw, stopped, maes, stop_hits, stop_dists, dates = [], [], [], 0, [], []
        rows = sess[(sess["gap_bucket"] == bucket) & (sess["f45_dir"] != 0)]
        for r in rows.itertuples():
            g = bars_by_date.get(r.date)
            if g is None:
                continue
            g = g.reset_index(drop=True)
            after = g.iloc[FIRST45_BARS:]
            if after.empty:
                continue
            entry = float(r.c0955)
            short = r.f45_dir > 0            # fade = opposite the morning
            sign = -1.0 if short else 1.0
            close = float(g["close"].iloc[-1])
            # dates appended in lockstep with raw — the year split must never
            # desync from the pnl list if a session is ever skipped above
            dates.append(r.date)
            raw.append(sign * (close - entry) / entry * 1e4)
            # MAE of the raw hold: worst excursion against the fade
            mae = ((float(after["high"].max()) - entry) if short
                   else (entry - float(after["low"].min()))) / entry * 1e4
            maes.append(max(mae, 0.0))
            # stopped variant: invalidation at the OR45 extreme
            hi = float(g["high"].iloc[:FIRST45_BARS].max())
            lo = float(g["low"].iloc[:FIRST45_BARS].min())
            stop = hi if short else lo
            stop_dists.append(abs(stop - entry) / entry * 1e4)
            exit_px, hit = close, False
            for b in after.itertuples():
                if short and float(b.open) >= stop:
                    exit_px, hit = float(b.open), True
                    break
                if short and float(b.high) >= stop:
                    exit_px, hit = stop, True
                    break
                if not short and float(b.open) <= stop:
                    exit_px, hit = float(b.open), True
                    break
                if not short and float(b.low) <= stop:
                    exit_px, hit = stop, True
                    break
            stopped.append(sign * (exit_px - entry) / entry * 1e4)
            stop_hits += 1 if hit else 0
        return raw, stopped, maes, stop_hits, stop_dists, dates

    raw, stopped, maes, stop_hits, stop_dists, dates = _run("big_up")
    if len(raw) < MIN_TENDENCY:
        return {"note": f"Only {len(raw)} qualifying big-gap-up mornings — "
                        f"no verdict under n={MIN_TENDENCY}."}
    year_pnls: dict = {}
    for pnl, date_s in zip(raw, dates):
        year_pnls.setdefault(date_s[:4], []).append(pnl)
    by_year = {y: _bt_cell(year_pnls[y]) for y in sorted(year_pnls)}
    mirror_raw = _run("big_down")[0]
    maes_sorted = sorted(maes)
    return {
        "rule": ("big gap up (>+45bps) and first-45 direction nonzero: enter at "
                 "the 10:00 freeze AGAINST that direction, exit at close; "
                 "stopped variant invalidates at the OR45 extreme"),
        "raw_hold_to_close": _bt_cell(raw),
        "stopped_at_or45_extreme": {
            **_bt_cell(stopped),
            "stop_hit_rate": round(stop_hits / len(stopped), 3) if stopped else None,
            "median_stop_distance_bps": (round(sorted(stop_dists)[len(stop_dists) // 2], 1)
                                         if stop_dists else None),
        },
        "mae_of_raw_hold_bps": {
            "median": round(maes_sorted[len(maes_sorted) // 2], 1) if maes else None,
            # nearest-rank on n-1 so a 10-sample p90 is the 9th value, not the max
            "p90": (round(maes_sorted[int((len(maes_sorted) - 1) * 0.9)], 1)
                    if maes else None),
        },
        # _raw suffix: these grade the HOLD-TO-CLOSE variant only — the year
        # split the verdict hangs on must say which arm it judged.
        "by_year_raw": by_year,
        # The noise check: the study says big-gap-DOWN days trend, so the same
        # fade rule there should LOSE. If it wins too, we found noise.
        "big_down_mirror_raw": _bt_cell(mirror_raw),
        "note": ("Index bps, no charges or slippage, entry at the 09:55 bar's "
                 "close print, conservative gap-through stops. This gates the "
                 "shadow-detector slot; the paper ledger is the net test."),
    }


# ---- live read + scoreboard (Phase 1) ---------------------------------------

def _prev_session(tail: pd.DataFrame, today_date: str):
    """Last stored session strictly before today, as {date, close, high, low}.
    The caller sees the DATE — a stale spine (sync not run) is visible, never
    silently treated as yesterday."""
    if tail.empty:
        return None
    pt = _with_ist(tail)
    pt = pt[pt["date"] < today_date]
    if pt.empty:
        return None
    last = pt[pt["date"] == pt["date"].iloc[-1]]
    return {"date": str(last["date"].iloc[0]),
            "close": float(last["close"].iloc[-1]),
            "high": float(last["high"].max()),
            "low": float(last["low"].min())}


def _range_levels(w: pd.DataFrame, bars: int):
    """OR levels only when the slice is REALLY the first `bars*5` minutes — a
    feed hole inside the forming window must not relabel 25 minutes as OR15."""
    if len(w) < bars:
        return None
    seg = w.iloc[:bars]
    mins = 9 * 60 + 15 + 5 * (bars - 1)
    if seg["hhmm"].iloc[0] != "09:15" or seg["hhmm"].iloc[-1] != f"{mins // 60:02d}:{mins % 60:02d}":
        return None
    return {"high": float(seg["high"].max()), "low": float(seg["low"].min())}


def assemble_opening_live(today: pd.DataFrame, tail: pd.DataFrame, results: dict) -> dict:
    """Today's forming opening state matched against the stored study tables.

    Pure assembly — no I/O and no clock: "window complete" means nine CLOSED
    bars starting 09:15, not wall time; closed bars are the only honest input.
    Mid-window values are labelled so_far/forming; matched cells appear as
    soon as the gap is classifiable (one closed bar).
    """
    op = results.get("opening") or {}
    out = {
        "study_sessions": op.get("sessions"),
        "note": ("Frequencies from the stored study, not forecasts. Features "
                 "freeze when the ninth bar closes at 10:00."),
    }
    if today.empty:
        out.update(bars_in_window=0, bars_today=0, window_complete=False,
                   state=None, matched=None, status="No closed bars yet today.")
        return out

    t = _with_ist(today.sort_values("ts").reset_index(drop=True))
    w = t.iloc[:FIRST45_BARS]
    aligned = w["hhmm"].iloc[0] == "09:15"
    # Contiguous = bar COUNT equals elapsed session time, so a bar index is a
    # valid index into time-of-day yardsticks (the pace curve). A feed hole
    # breaks that equivalence even when the open is aligned.
    mins = 9 * 60 + 15 + 5 * (len(w) - 1)
    contiguous = bool(aligned and w["hhmm"].iloc[-1] == f"{mins // 60:02d}:{mins % 60:02d}")
    complete = bool(contiguous and len(w) == FIRST45_BARS)
    prev = _prev_session(tail, t["date"].iloc[0])

    o = float(w["open"].iloc[0]) if aligned else None
    gap_bps = gap_bucket = None
    if o is not None and prev is not None:
        gap_bps = _bps((o - prev["close"]) / prev["close"])
        gap_bucket = _gap_bucket(gap_bps)

    c_last = float(w["close"].iloc[-1])
    state = {
        "prev_session": prev,
        "aligned_0915": bool(aligned),
        "gap_bps": gap_bps,
        "gap_bucket": gap_bucket,
        "or15": _range_levels(w, OR15_BARS),
        "or30": _range_levels(w, OR30_BARS),
        "or45": {"high": float(w["high"].max()), "low": float(w["low"].min()),
                 "complete": complete},
        "f45_dir_so_far": int(np.sign(c_last - o)) if o is not None else None,
        "last_close": c_last,
    }
    if o is not None:
        state["or45_bps_so_far"] = _bps((float(w["high"].max())
                                         - float(w["low"].min())) / o)
        terc = op.get("or45_tercile_bps")
        if complete and terc:
            v = state["or45_bps_so_far"]
            state["or45_tercile"] = ("small" if v <= terc[0]
                                     else "large" if v >= terc[1] else "mid")
    # VWAP and pace only on a contiguous 09:15-anchored window: the pace curve
    # is indexed by bar-of-day, and build_sessions would disqualify a
    # misaligned window outright — a wrong NUMBER is worse than no number
    # (review catch: a 09:30-anchored morning read pace 1.0 where truth was
    # 0.75, because bar count is not time-of-day when the anchor slips).
    vsum = float(w["vol_proxy"].sum())
    if vsum > 0 and contiguous:
        tp = (w["high"] + w["low"] + w["close"]) / 3
        state["above_proxy_vwap"] = bool(c_last >= float((tp * w["vol_proxy"]).sum() / vsum))
        curve = (results.get("volume_pace") or {}).get("cum_median") or []
        i = min(len(w) - 1, len(curve) - 1, FIRST45_BARS - 1)
        if curve and i >= 0 and curve[i] > 0:
            state["pace_vs_typical"] = round(vsum / curve[i], 2)

    matched = None
    if op:
        # The unconditional cells ride along even when the gap is unreadable
        # (no prev close / misaligned open) — a stale spine hides the gap
        # cells, not the whole study.
        matched = {"or30_breakout": op.get("or30_breakout")}
        if gap_bucket is not None:
            matched.update(
                gap=(op.get("gap") or {}).get("by_bucket", {}).get(gap_bucket),
                continuation=(op.get("continuation_10_to_close") or {})
                             .get("by_gap", {}).get(gap_bucket),
                trend_day=(op.get("trend_day") or {}).get("by_gap", {}).get(gap_bucket),
            )
        terc_key = state.get("or45_tercile")
        if terc_key:
            matched["continuation_by_or45"] = ((op.get("continuation_10_to_close") or {})
                                              .get("by_or45_size", {}).get(terc_key))
            matched["trend_day_by_or45"] = ((op.get("trend_day") or {})
                                            .get("by_or45_size", {}).get(terc_key))
    out.update(bars_in_window=int(len(w)), bars_today=int(len(t)),
               window_complete=complete, state=state, matched=matched)
    return out


def recent_mornings(df: pd.DataFrame, op: dict, n: int = 10) -> dict:
    """The scoreboard: the last n graded mornings vs the study's matched cells.

    Graded OFFLINE from the stored spine — the frozen 10:00 state is fully
    reconstructable from stored bars, so there is no live capture to lose on
    a restart and nothing new to persist. Call on exclude_current_day(df):
    outcomes are final only at the close.
    """
    sess = build_sessions(df)
    if sess.empty:
        return {"mornings": [], "headline": [], "note": "No gradable sessions."}
    rows = []
    for r in sess.tail(n).itertuples():
        continued = bool(r.drift * r.f45_dir > 0) if r.f45_dir else None
        gradable_fill = r.gap_bucket not in (None, "flat")
        rows.append({
            "date": r.date,
            # float64 column: a no-prev-close row holds NaN, which json.dumps
            # would emit as spec-invalid literal NaN — wrap to None.
            "gap_bps": None if pd.isna(r.gap_bps) else float(r.gap_bps),
            "gap_bucket": r.gap_bucket,
            "or45_bps": r.or45_bps,
            "outcomes": {
                "gap_faded": r.gap_faded,
                "filled_by_close": (bool(not pd.isna(r.fill_idx))
                                    if gradable_fill else None),
                "continued_to_close": continued,
                "trend_day": bool(r.trend_day),
            },
            "matched": {
                "gap": (op.get("gap") or {}).get("by_bucket", {}).get(r.gap_bucket),
                "continuation": (op.get("continuation_10_to_close") or {})
                                .get("by_gap", {}).get(r.gap_bucket),
                "trend_day": (op.get("trend_day") or {}).get("by_gap", {})
                             .get(r.gap_bucket),
            },
        })
    # The three headline tendencies, tallied over the same window. Small-n by
    # construction — the note says so rather than the tally pretending.
    tail = sess.tail(n)
    heads = []
    big = tail[tail["gap_bucket"].isin(["big_up", "big_down"])]
    if len(big):
        heads.append({"tendency": "big gaps don't fill by close",
                      "n": int(len(big)),
                      "hits": int(big["fill_idx"].isna().sum())})
    bu = tail[(tail["gap_bucket"] == "big_up") & (tail["f45_dir"] != 0)]
    if len(bu):
        heads.append({"tendency": "big gap-up morning fades to close",
                      "n": int(len(bu)),
                      "hits": int((bu["drift"] * bu["f45_dir"] <= 0).sum())})
    bd = tail[tail["gap_bucket"] == "big_down"]
    if len(bd):
        heads.append({"tendency": "big gap-down day trends",
                      "n": int(len(bd)), "hits": int(bd["trend_day"].sum())})
    return {
        "mornings": rows,
        "headline": heads,
        "note": ("Graded offline from the stored spine; a graded session may "
                 "itself sit inside the study tables (weight 1/n_study). "
                 "CAS era (03-Aug-2026 on): the session's last bar carries the "
                 "auction print, so close-judged outcomes include it while the "
                 "matched tables are mostly pre-CAS history. Headline tallies "
                 "are observational — judge at 30+, like everything else here."),
    }


def opening_study(df: pd.DataFrame) -> dict:
    """The full Phase-0 table set. Call on exclude_current_day(df) only."""
    sess = build_sessions(df)
    if len(sess) < MIN_TENDENCY:
        return {"note": f"Only {len(sess)} qualifying sessions — nothing may be "
                        f"called at n < {MIN_TENDENCY}."}
    or45_q = (float(sess["or45_bps"].quantile(1 / 3)),
              float(sess["or45_bps"].quantile(2 / 3)))
    return {
        "window": "09:15-10:00 IST — nine 5-min bars; features frozen at 10:00",
        "sessions": {"n": int(len(sess)), "from": sess["date"].iloc[0],
                     "to": sess["date"].iloc[-1]},
        "volatility": _volatility_profile(df, sess),
        "gap": _gap_cells(sess),
        "or30_breakout": _or30_breakout_cells(sess),
        "continuation_10_to_close": _continuation_cells(sess, or45_q),
        "trend_day": _trend_day_cells(sess, or45_q),
        "or45_tercile_bps": [round(or45_q[0], 1), round(or45_q[1], 1)],
        # Phase-3 gate: the fade rule backtested with a stop, a year split and
        # the big-down mirror — decides whether it earns a shadow-detector slot.
        "fade_backtest": fade_backtest(df, sess),
        "note": (
            "Historical frequencies, not predictions. Continuation cells are "
            "signed returns on the coin band (45-55% = coin); rate cells are "
            "judged against their own base rate, not 50%. VWAP is the NIFTYBEES "
            "proxy. No expiry-day conditioning yet — see module docstring."
        ),
    }
