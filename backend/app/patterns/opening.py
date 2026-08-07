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
        "note": (
            "Historical frequencies, not predictions. Continuation cells are "
            "signed returns on the coin band (45-55% = coin); rate cells are "
            "judged against their own base rate, not 50%. VWAP is the NIFTYBEES "
            "proxy. No expiry-day conditioning yet — see module docstring."
        ),
    }
