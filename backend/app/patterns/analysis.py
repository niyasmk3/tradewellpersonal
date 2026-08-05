"""Day-of-week analytics on 5-minute NIFTY bars.

Every stat ships with its sample count (n) — a "Monday tendency" seen on 30 of
50 Mondays is a 60% coin, and the results JSON must make that visible instead
of presenting folklore. Returns are in basis points of the day's open unless
noted.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.patterns.detect import PATTERNS, detect

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
FWD_BARS = 6          # 30-min forward window for pattern outcome scoring
SLOT_MINUTES = 30     # intraday time-of-day resolution


def _with_ist(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ist = pd.to_datetime(out["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    out["date"] = ist.dt.date.astype(str)
    out["weekday"] = ist.dt.weekday          # 0=Mon
    out["hhmm"] = ist.dt.strftime("%H:%M")
    slot_min = (ist.dt.hour * 60 + ist.dt.minute) // SLOT_MINUTES * SLOT_MINUTES
    out["slot"] = (slot_min // 60).astype(str).str.zfill(2) + ":" + (slot_min % 60).astype(str).str.zfill(2)
    return out


def build_daily(df: pd.DataFrame) -> pd.DataFrame:
    """One row per trading day, from the 5-min spine."""
    g = _with_ist(df).groupby("date", sort=True)
    daily = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "vol_proxy": g["vol_proxy"].sum(),
        "weekday": g["weekday"].first(),
        "bars": g.size(),
    })
    # First-hour (09:15-10:15) range share of the day's range.
    first_hour = _with_ist(df)[lambda d: d["hhmm"] < "10:15"].groupby("date")
    daily["fh_high"] = first_hour["high"].max()
    daily["fh_low"] = first_hour["low"].min()
    # Only full sessions (75 bars) count toward day-of-week stats: a live
    # partial day or a disrupted session would register as a fake small-range,
    # low-volume day and skew its weekday's averages.
    daily = daily[daily["bars"] >= 70]
    daily["prev_close"] = daily["close"].shift(1)
    return daily.reset_index()


def _bps(x) -> float:
    return round(float(x) * 10000, 1)


def day_of_week_stats(daily: pd.DataFrame) -> dict:
    out = {}
    d = daily.dropna(subset=["prev_close"]).copy()
    d["oc_ret"] = (d["close"] - d["open"]) / d["open"]
    d["gap"] = (d["open"] - d["prev_close"]) / d["prev_close"]
    d["day_range"] = (d["high"] - d["low"]) / d["open"]
    d["fh_share"] = ((d["fh_high"] - d["fh_low"]) / (d["high"] - d["low"]).clip(lower=1e-9)).clip(upper=1.0)
    # Trend day: closes in the outer quarter of its range (either side).
    span = (d["high"] - d["low"]).clip(lower=1e-9)
    pos = (d["close"] - d["low"]) / span
    d["trend_day"] = (pos >= 0.75) | (pos <= 0.25)
    d["gap_faded"] = np.where(d["gap"] > 0, d["close"] < d["open"], d["close"] > d["open"])

    for wd in range(5):
        g = d[d["weekday"] == wd]
        n = len(g)
        if n == 0:
            continue
        up = int((g["oc_ret"] > 0).sum())
        gap_up = g[g["gap"] > 0.0005]
        gap_dn = g[g["gap"] < -0.0005]
        out[WEEKDAYS[wd]] = {
            "n_days": n,
            "open_to_close_bps": {"mean": _bps(g["oc_ret"].mean()), "median": _bps(g["oc_ret"].median())},
            "up_days": up,
            "up_day_rate": round(up / n, 3),
            "gap_bps": {"mean": _bps(g["gap"].mean()), "abs_mean": _bps(g["gap"].abs().mean())},
            "gap_up_days": int(len(gap_up)),
            "gap_up_faded_rate": round(float(gap_up["gap_faded"].mean()), 3) if len(gap_up) else None,
            "gap_down_days": int(len(gap_dn)),
            "gap_down_faded_rate": round(float(gap_dn["gap_faded"].mean()), 3) if len(gap_dn) else None,
            "avg_range_bps": _bps(g["day_range"].mean()),
            "first_hour_range_share": round(float(g["fh_share"].mean()), 3),
            "trend_day_rate": round(float(g["trend_day"].mean()), 3),
        }
    return out


def time_of_day_profile(df: pd.DataFrame) -> dict:
    """Per weekday x 30-min slot: direction bias, avg move, volume-proxy share.
    This is where 'Monday 11:00 tends to drift down' claims live — with n."""
    b = _with_ist(df).copy()
    b["ret"] = (b["close"] - b["open"]) / b["open"]
    has_vol = float(b["vol_proxy"].sum()) > 0

    out = {}
    for wd in range(5):
        g = b[b["weekday"] == wd]
        if g.empty:
            continue
        slots = []
        day_vol = g.groupby("date")["vol_proxy"].sum()
        for slot, sg in g.groupby("slot"):
            per_day = sg.groupby("date")["ret"].apply(lambda r: (1 + r).prod() - 1)
            n = len(per_day)
            if n < 10:
                continue
            slot_share = None
            if has_vol:
                sv = sg.groupby("date")["vol_proxy"].sum()
                share = (sv / day_vol.reindex(sv.index).clip(lower=1)).mean()
                slot_share = round(float(share), 4)
            slots.append({
                "slot": slot,
                "n_days": int(n),
                "mean_ret_bps": _bps(per_day.mean()),
                "up_rate": round(float((per_day > 0).mean()), 3),
                "mean_abs_ret_bps": _bps(per_day.abs().mean()),
                "vol_proxy_share": slot_share,
            })
        out[WEEKDAYS[wd]] = slots
    return out


CAS_LIVE_FROM = "2026-08-03"   # NSE Closing Auction Session go-live


def auction_print(df: pd.DataFrame) -> dict:
    """The CAS auction-print series: official close vs the last free tape.

    Under the closing-auction regime the index freezes ~15:15 while the 50
    stocks auction; the official close is then recomputed from auction
    prices — and Kite writes that print INTO the session's last intraday bar
    (verified 03-Aug: the 15:25 bar closes at 24,774 while the frozen bars
    before it sit at 24,573). So, offline from the 5-min spine alone:
      tape     = close of the 15:10 bar (last bar fully before the freeze)
      official = close of the session's final bar (carries the print)
      print    = official - tape
    WHY IT MATTERS: nothing intraday can trade the print (F&O books close
    15:30) — but on EXPIRY DAY options settle against the official close,
    print included. If the series shows a systematic bias, that is real
    money on Tuesday expiries. Pre-CAS days use the same formula and should
    hover near zero — the built-in sanity check on the method.
    """
    d = _with_ist(df)
    rows = []
    for date_s, g in d.groupby("date", sort=True):
        b1510 = g[g["hhmm"] == "15:10"]
        last = g.iloc[-1]
        if not len(b1510) or last["hhmm"] < "15:20":
            continue                     # partial/disrupted session
        tape = float(b1510["close"].iloc[0])
        official = float(last["close"])
        rows.append({"date": date_s, "tape": round(tape, 2),
                     "official": round(official, 2),
                     "print": round(official - tape, 2)})

    cas = [r for r in rows if r["date"] >= CAS_LIVE_FROM]
    pre = [r for r in rows if r["date"] < CAS_LIVE_FROM]

    def _stats(rs):
        if not rs:
            return None
        moves = [r["print"] for r in rs]
        return {
            "n": len(moves),
            "mean": round(sum(moves) / len(moves), 1),
            "median": round(sorted(moves)[len(moves) // 2], 1),
            "positive_rate": round(100 * sum(1 for m in moves if m > 0) / len(moves), 1),
            "max_abs": round(max(abs(m) for m in moves), 1),
        }

    return {
        "method": ("official close (session's last 5m bar, which Kite backfills "
                   "with the auction print) minus the 15:10 bar close (last free "
                   "tape before the freeze)"),
        "cas_live_from": CAS_LIVE_FROM,
        "note": ("Untradeable intraday (F&O closes 15:30, the auction is stocks-"
                 "only) — but EXPIRY-DAY settlement uses the official close, "
                 "print included. Judge the bias at 20+ CAS sessions, not 3."),
        "cas_days": cas[-30:],
        "cas_stats": _stats(cas),
        "pre_cas_baseline": _stats(pre),
    }


def pattern_frequency(df: pd.DataFrame) -> dict:
    """Candlestick pattern counts per weekday + forward-outcome scoring.

    Outcome = close[t+FWD_BARS] vs close[t], in the pattern's signaled
    direction, only when the full window stays inside the same session.
    Neutral patterns (doji/spinning top/inside bar) get frequency only.
    """
    det = detect(df)
    det = _with_ist(det)
    fwd = det["close"].shift(-FWD_BARS) / det["close"] - 1
    same_sess = det["date"].eq(det["date"].shift(-FWD_BARS))

    out = {}
    for name, (direction, _) in PATTERNS.items():
        mask = det[name]
        total = int(mask.sum())
        if total == 0:
            continue
        by_wd = {}
        for wd in range(5):
            m = mask & det["weekday"].eq(wd)
            n = int(m.sum())
            if n == 0:
                continue
            entry = {"count": n}
            if direction != 0:
                ok = m & same_sess & fwd.notna()
                if int(ok.sum()) >= 10:
                    signed = fwd[ok] * direction
                    entry["n_scored"] = int(ok.sum())
                    entry["hit_rate_30m"] = round(float((signed > 0).mean()), 3)
                    entry["avg_fwd_30m_bps"] = _bps(signed.mean())
            by_wd[WEEKDAYS[wd]] = entry
        overall = {"count": total, "direction": {1: "bullish", -1: "bearish", 0: "neutral"}[direction]}
        if direction != 0:
            ok = mask & same_sess & fwd.notna()
            if int(ok.sum()) >= 10:
                signed = fwd[ok] * direction
                overall["n_scored"] = int(ok.sum())
                overall["hit_rate_30m"] = round(float((signed > 0).mean()), 3)
                overall["avg_fwd_30m_bps"] = _bps(signed.mean())
        out[name] = {"overall": overall, "by_weekday": by_wd}
    # Most-repeated first — the user's headline ask.
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["overall"]["count"]))
