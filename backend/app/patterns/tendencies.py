"""Volume-conditioned pattern tendencies + the live tape read.

WHAT THIS IS: the module already counts candlestick patterns and scores a
30-minute forward outcome. This layer answers the two questions that number
alone cannot: (1) does PARTICIPATION change the odds — the same engulfing bar
on 2x volume vs a dead tape; and (2) what does TODAY's forming tape look like
against those 3-year conditional frequencies.

WHAT THIS IS NOT: prediction. Every cell is a historical frequency with its n
attached, and the classical research this module's detectors follow found
intraday candlestick edges hovering near coin-flip. When a cell reads 51% on
n=800, the honest output is "coin", and this file says so. The volume proxy is
NIFTYBEES ETF volume (indices print none) — relative shape only.

Cell discipline (same as analysis.py): nothing is scored under n=10, nothing
is called a tendency under n=30, and a tendency needs a hit rate outside
45-55% — inside that band the label is "coin" no matter how big the sample.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.patterns.detect import PATTERNS, VOL_LOOKBACK, detect

# Bar's proxy volume vs its rolling 20-bar median: the same relative-to-recent
# yardstick the detectors use for range, so regimes hold across 3 years of ETF
# AUM drift. Thresholds match the signal engine's own volume-ratio buckets.
VOL_HIGH = 1.5
VOL_LOW = 0.7

HORIZONS = {"15m": 3, "30m": 6, "60m": 12}
MIN_CELL = 10          # below this a cell is omitted entirely
MIN_TENDENCY = 30      # below this nothing may be called a tendency
COIN_BAND = (0.45, 0.55)

# Live read: patterns are reported from the last 6 closed bars (30 minutes) —
# older than that they are chart history, not a current read.
RECENT_BARS = 6
PACE_DAYS = 60         # sessions used for the typical cumulative-volume curve


def _ist(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")


def volume_regime(vol: pd.Series) -> pd.Series:
    """Per-bar participation bucket: "high" / "normal" / "low", or None when
    the proxy is absent (0) — an absent proxy must not masquerade as a dead
    tape (the engine's own P0-1 lesson)."""
    med = vol.rolling(VOL_LOOKBACK, min_periods=5).median()
    out = pd.Series([None] * len(vol), index=vol.index, dtype=object)
    ok = (vol > 0) & (med > 0)
    out[ok & (vol >= VOL_HIGH * med)] = "high"
    out[ok & (vol <= VOL_LOW * med)] = "low"
    out[ok & (vol > VOL_LOW * med) & (vol < VOL_HIGH * med)] = "normal"
    return out


def _bps(x) -> float:
    return round(float(x) * 10000, 1)


def _verdict(n: int, raw_hit: float) -> str:
    if n < MIN_TENDENCY:
        return "sample too small"
    lo, hi = COIN_BAND
    if lo <= raw_hit <= hi:
        return "coin"
    return "tendency"


def _cell(signed: pd.Series) -> dict:
    # The verdict is judged on the RAW fraction, before display rounding —
    # 899/2000 = 0.4495 is outside the band and must not become "coin" just
    # because it prints as 45.0% (review catch).
    raw = float((signed > 0).mean())
    return {
        "n": int(len(signed)),
        "hit_rate": round(raw, 3),
        "avg_bps": _bps(signed.mean()),
        "median_bps": _bps(signed.median()),
        "verdict": _verdict(int(len(signed)), raw),
    }


def exclude_current_day(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the current IST day's bars — the 'historical' table the live read
    compares TODAY against must not contain today's own already-realized
    outcomes (review catch: a mid-session re-analyze would grade the morning's
    patterns and then serve them back as independent history that afternoon).
    """
    if df.empty:
        return df
    import time as _t

    today = _t.strftime("%Y-%m-%d", _t.gmtime(int(_t.time()) + 19800))
    return df[_ist(df).dt.strftime("%Y-%m-%d") < today]


def conditional_outcomes(df: pd.DataFrame) -> dict:
    """Directional pattern x volume regime x horizon forward-outcome table.

    Outcomes are signed by the pattern's textbook direction and scored only
    when the full horizon stays inside the same session (an overnight gap is
    a different animal, analyzed in day_of_week_stats).
    """
    det = detect(df)
    date = _ist(det).dt.date
    regime = volume_regime(det["vol_proxy"])

    out = {}
    for name, (direction, _) in PATTERNS.items():
        if direction == 0:
            continue                     # neutral patterns have no signed outcome
        mask = det[name]
        if int(mask.sum()) == 0:
            continue
        horizons = {}
        for label, bars in HORIZONS.items():
            fwd = det["close"].shift(-bars) / det["close"] - 1
            ok = mask & fwd.notna() & date.eq(date.shift(-bars))
            if int(ok.sum()) < MIN_CELL:
                continue
            signed = fwd[ok] * direction
            h = {"all": _cell(signed)}
            by_vol = {}
            for reg in ("high", "normal", "low"):
                sel = ok & regime.eq(reg)
                if int(sel.sum()) >= MIN_CELL:
                    by_vol[reg] = _cell(fwd[sel] * direction)
            if by_vol:
                h["by_volume"] = by_vol
            # The question this table exists for, answered only when both
            # ends have a real sample: does participation change the odds?
            hi, lo = by_vol.get("high"), by_vol.get("low")
            if hi and lo and hi["n"] >= MIN_TENDENCY and lo["n"] >= MIN_TENDENCY:
                h["volume_effect_pp"] = round(100 * (hi["hit_rate"] - lo["hit_rate"]), 1)
            horizons[label] = h
        if horizons:
            out[name] = {
                "direction": "bullish" if direction > 0 else "bearish",
                "n_total": int(mask.sum()),
                "horizons": horizons,
            }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n_total"]))


def volume_pace_curve(df: pd.DataFrame, days: int = PACE_DAYS) -> dict:
    """Median cumulative proxy volume by bar-of-day over the last `days` full
    sessions — the yardstick "today is running at 1.3x typical" needs."""
    d = df.copy()
    ist = _ist(d)
    d["date"] = ist.dt.date.astype(str)
    sizes = d.groupby("date").size()
    vols = d.groupby("date")["vol_proxy"].sum()
    full = sizes[(sizes >= 70) & (vols.reindex(sizes.index).fillna(0) > 0)].index
    recent = sorted(full)[-days:]
    if not recent:
        return {"days": 0, "cum_median": []}
    d = d[d["date"].isin(recent)]
    d["bar_idx"] = d.groupby("date").cumcount()
    d["cum"] = d.groupby("date")["vol_proxy"].cumsum()
    curve = d.groupby("bar_idx")["cum"].median()
    return {"days": int(len(recent)), "cum_median": [round(float(x), 0) for x in curve]}


# ---- live read --------------------------------------------------------------

def assemble_live_read(today: pd.DataFrame, tail: pd.DataFrame, results: dict) -> dict:
    """Pure assembly: today's closed bars + stored history tail + the analyzed
    conditional table -> the current tape read. No I/O, fully testable.

    `today` needs ts/open/high/low/close/vol_proxy for TODAY's closed bars;
    `tail` is the stored spine's last ~60 bars for detector warm-up (rolling
    medians and the trend EMA need context or the first bars of the session
    misclassify).
    """
    table = results.get("conditional_outcomes") or {}
    pace = results.get("volume_pace") or {}
    if today.empty:
        return {"bars_today": 0, "patterns": [], "volume": None,
                "note": "No closed bars yet today."}

    tail = tail[tail["ts"] < int(today["ts"].min())]
    frame = pd.concat([tail, today], ignore_index=True).sort_values("ts").reset_index(drop=True)
    det = detect(frame)
    regime = volume_regime(det["vol_proxy"])
    hhmm = _ist(det).dt.strftime("%H:%M")
    n_today = len(today)

    fired = []
    recent = det.iloc[-min(RECENT_BARS, n_today):]
    for idx in recent.index:
        for name, (direction, _) in PATTERNS.items():
            if direction == 0 or not bool(det.at[idx, name]):
                continue
            reg = regime.at[idx]
            stats = (table.get(name) or {}).get("horizons", {})
            h30 = stats.get("30m") or {}
            cond = (h30.get("by_volume") or {}).get(reg) if reg else None
            fired.append({
                "bar": hhmm.at[idx],
                # Epoch of the bar's open — what a chart marker anchors to.
                "bar_ts": int(det.at[idx, "ts"]),
                "pattern": name,
                "direction": "bullish" if direction > 0 else "bearish",
                "volume_regime": reg,
                # The matching historical cell (volume-conditioned when the
                # bar's regime has one; the unconditioned row always rides
                # along for scale) — frequencies, not forecasts.
                "historical_30m": cond or h30.get("all"),
                "historical_30m_all": h30.get("all"),
                "conditioned": bool(cond),
                "horizons": {k: v.get("all") for k, v in stats.items()} or None,
            })

    vol = None
    if float(today["vol_proxy"].sum()) > 0:
        cum = float(today["vol_proxy"].sum())
        curve = pace.get("cum_median") or []
        i = min(n_today - 1, len(curve) - 1)
        pace_ratio = round(cum / curve[i], 2) if (curve and i >= 0 and curve[i] > 0) else None
        # Trend of participation: last `half` bars vs the prior `half`. Early
        # in the session the window shrinks — the payload SAYS so via
        # trend_window_min instead of a key that claims 30 minutes it does
        # not have (review catch: label honesty, the module's whole ethos).
        half = min(RECENT_BARS, max(1, n_today // 2))
        last = float(today["vol_proxy"].iloc[-half:].sum())
        prior = float(today["vol_proxy"].iloc[-2 * half:-half].sum()) if n_today >= 2 * half else 0.0
        trend = None
        if prior > 0:
            r = last / prior
            trend = "rising" if r >= 1.25 else "falling" if r <= 0.8 else "flat"
        vol = {
            "current_regime": regime.iloc[-1],
            "pace_vs_typical": pace_ratio,
            "pace_days": pace.get("days"),
            "trend": trend,
            "trend_window_min": half * 5 if trend else None,
        }

    return {
        "bars_today": int(n_today),
        "last_bar": hhmm.iloc[-1],
        "last_close": float(det["close"].iloc[-1]),
        "patterns": fired,
        "volume": vol,
        # Today's closed bars, verbatim — the Lab's tape chart draws these and
        # pins each pattern/callout marker to its bar_ts. Index-space, so the
        # S/R ladder overlays exactly (no futures-basis fudge).
        "candles": [
            {"ts": int(r.ts), "open": float(r.open), "high": float(r.high),
             "low": float(r.low), "close": float(r.close)}
            for r in today.itertuples()
        ],
        "note": (
            "Historical conditional frequencies, not predictions — a 52% cell "
            "is a coin. Volume is the NIFTYBEES proxy (relative shape only)."
        ),
    }


def fetch_today(kite) -> pd.DataFrame:
    """Today's CLOSED 5-min session bars with proxy volume, via two Kite
    intraday calls. Import-light and kept out of assemble_live_read so the
    assembly stays pure."""
    import time
    from datetime import datetime

    from app.patterns.data import NIFTY_TOKEN, _in_session, resolve_proxy_token

    day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    now_dt = datetime.now()
    rows = []
    for d in kite.historical_data(NIFTY_TOKEN, day_start, now_dt, "5minute"):
        dt = d["date"]
        if not _in_session(dt):
            continue
        epoch = int(dt.timestamp())
        if epoch + 300 > int(time.time()):
            continue                     # forming bar — a live read uses closed bars
        rows.append({"ts": epoch, "open": d["open"], "high": d["high"],
                     "low": d["low"], "close": d["close"], "vol_proxy": 0.0})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    try:
        proxy = resolve_proxy_token(kite)
        vols = {int(d["date"].timestamp()): float(d.get("volume", 0) or 0)
                for d in kite.historical_data(proxy, day_start, now_dt, "5minute")}
        df["vol_proxy"] = df["ts"].map(vols).fillna(0.0)
    except Exception:
        pass                             # OHLC read stands on its own (same rule as sync)
    return df
