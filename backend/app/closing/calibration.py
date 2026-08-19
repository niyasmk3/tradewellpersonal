"""Fit the bridge from real index data to a weekly ATM option price.

The study prices contracts that no longer exist, from two things that do:
NIFTY spot and India VIX. Two gaps sit between those and a premium, and both
are MEASURED here against real quotes rather than assumed.

  GAP 1 — CARRY.  Options are priced off the forward, not the index. Put-call
    parity on real ATM quotes puts the NIFTY forward ~11-12%/yr above spot,
    well above the 6.5% risk-free the rest of the codebase carries. Ignoring
    that overprices puts and underprices calls by ~25% of an ATM premium at
    1 DTE, which is a directional bias in a study whose whole subject is
    direction. Fitted as ONE pooled rate: in this sample each DTE bucket is
    essentially one session, so a per-DTE basis curve would be fitting six
    days of noise.

  GAP 2 — VOL.  India VIX is a 30-day constant-maturity number; the trade is
    in a 0-7 DTE contract. Measured, weekly ATM IV runs ~1.5x VIX on expiry
    day and ~0.85x a week out. This one IS fitted per DTE, because the term
    structure is a within-day effect visible in every session, not a
    between-day one.

Stage order matters: carry first, then IV re-solved against the corrected
forward. The `iv` column already stored on chain_snap was solved at r=6.5% and
is deliberately NOT reused — it would bake the old forward back in.

Source for both: app/condor's chain_snap table, the only real NIFTY option
quotes on this machine. Everything here inherits that sample's size and its
single volatility regime; see `caveats` on each fit.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

from app.closing import store
from app.closing.calendar import ist_date, ist_minutes, settlement_ts
from app.options.iv import implied_vol

log = logging.getLogger("tradewell.closing")

CHAIN_DB = Path(__file__).resolve().parents[2] / ".condor_chain.db"

# DTE edges the vol ratio is reported on. Fine near expiry (where the curve
# moves fastest and the study's exit leg lives), coarse further out.
_DTE_BUCKETS = [0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 9.0, 12.0, 15.0]

MIN_OBS_PER_BUCKET = 15   # below this a bucket median is one session's noise
_PARITY_MIN_DTE = 0.5     # expiry-day parity is pin-dominated, not carry
_YEAR_DAYS = 365.0


def _bucket(dte: float) -> Optional[float]:
    if dte < 0:
        return None
    lo = _DTE_BUCKETS[0]
    for edge in _DTE_BUCKETS:
        if dte >= edge:
            lo = edge
        else:
            break
    return lo


def _vix_lookup() -> dict:
    df = store.load_vix()
    return dict(zip(df["ts"].astype(int), df["close"].astype(float)))


def _vix_at(vix: dict, ts: int) -> Optional[float]:
    """VIX close of the 5-min bar containing ts, walking back up to 15 min for
    a snapshot that landed in a gap."""
    base = ts - (ts % 300)
    for cand in (base, base - 300, base - 600, base - 900):
        v = vix.get(cand)
        if v:
            return v
    return None


def _atm_observations(db_path: Path = CHAIN_DB,
                      exclude_sessions: Optional[set] = None) -> list:
    """One ATM row per (snapshot, expiry), from real quotes.

    ATM is located by minimum |CE - PE| on the reference price rather than by
    rounding a spot: it needs no spot, and it is what a trader's eye does.
    The reference price is the bid-ask mid where a two-sided quote exists and
    the ltp otherwise — a mid cannot be stale the way a last print can.

    Returns tuples:
      (ts, expiry, dte, strike, ce_ref, pe_ref, ce_ltp, pe_ltp,
       spread_pct, spread_abs)

    `exclude_sessions` holds out IST session dates (ISO strings) so callers can
    refit without them — the leave-one-session-out check in validate.py is the
    only honest read of this model's error, and it needs the fit to genuinely
    never see the held-out day.
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT ts, expiry, strike, right, ltp, bid, ask FROM chain_snap "
            "WHERE symbol = 'NIFTY' AND ltp IS NOT NULL AND ltp > 0"
        ).fetchall()
    finally:
        conn.close()

    grouped = defaultdict(dict)
    held = exclude_sessions or set()
    for ts, expiry, strike, right, ltp, bid, ask in rows:
        if not expiry or (held and ist_date(ts).isoformat() in held):
            continue
        ref = (bid + ask) / 2.0 if (bid and ask and ask > bid > 0) else float(ltp)
        grouped[(int(ts), expiry)][(float(strike), right)] = (ref, float(ltp), bid, ask)

    out = []
    for (ts, expiry), legs in grouped.items():
        best = None
        for (strike, right) in list(legs):
            if right != "CE":
                continue
            ce, pe = legs.get((strike, "CE")), legs.get((strike, "PE"))
            if not ce or not pe:
                continue
            gap = abs(ce[0] - pe[0])
            if best is None or gap < best[0]:
                best = (gap, strike, ce, pe)
        if best is None:
            continue
        _, strike, ce, pe = best
        try:
            exp_d = date.fromisoformat(expiry)
        except ValueError:
            continue
        dte = (settlement_ts(exp_d) - ts) / 86400.0

        spreads = []
        for leg in (ce, pe):
            _ref, _ltp, bid, ask = leg
            if bid and ask and ask > bid > 0:
                spreads.append(((ask - bid) / ((ask + bid) / 2.0), ask - bid))
        out.append((ts, expiry, dte, strike, ce[0], pe[0], ce[1], pe[1],
                    statistics.median(s[0] for s in spreads) if spreads else None,
                    statistics.median(s[1] for s in spreads) if spreads else None))
    return out


def fit_carry(spot_at, db_path: Path = CHAIN_DB,
              exclude_sessions: Optional[set] = None) -> dict:
    """Pooled continuously-compounded carry implied by ATM put-call parity.

    `spot_at(ts) -> float | None` supplies the index level; the caller owns it
    so this module never reaches into the candle store.

    C - P = e^{-rT}(F - K), so F = K + (C - P)e^{rT}; with rT tiny at these
    horizons the correction is second-order and F = K + (C - P) is used.
    """
    obs = _atm_observations(db_path, exclude_sessions)
    per_dte, pooled, sessions = defaultdict(list), [], set()
    for ts, _exp, dte, strike, ce, pe, _cl, _pl, _sp, _sa in obs:
        if dte <= _PARITY_MIN_DTE:
            continue
        s = spot_at(int(ts))
        if not s or s <= 0:
            continue
        fwd = strike + (ce - pe)
        if fwd <= 0:
            continue
        rate = math.log(fwd / s) / (dte / _YEAR_DAYS)
        per_dte[_bucket(dte)].append(rate)
        pooled.append(rate)
        sessions.add(ist_date(ts).isoformat())

    if not pooled:
        return {"rate": None, "n": 0, "sessions": 0, "by_dte": {}, "caveats": [
            "No real quotes available — pricing falls back to the codebase's "
            "6.5% risk-free, which understates the observed NIFTY forward."]}

    return {
        "rate": round(statistics.median(pooled), 4),
        "rate_pct": round(statistics.median(pooled) * 100, 2),
        "n": len(pooled),
        "sessions": len(sessions),
        "iqr_pct": [round(sorted(pooled)[len(pooled) // 4] * 100, 2),
                    round(sorted(pooled)[3 * len(pooled) // 4] * 100, 2)],
        "by_dte": {str(k): {"n": len(v), "rate_pct": round(statistics.median(v) * 100, 2)}
                   for k, v in sorted(per_dte.items())},
        "caveats": [
            f"One pooled rate fitted over {len(sessions)} sessions. The per-DTE "
            "spread below is mostly between-day variation, not a term "
            "structure — each bucket is roughly one session.",
            "Expiry-day observations (DTE <= 0.5) are excluded: near settlement "
            "parity is dominated by pinning, not carry.",
            "Carry drifts with the dividend calendar. A rate fitted in August "
            "is weakest across February-May, NIFTY's heaviest dividend season.",
        ],
    }


def fit_iv_curve(spot_at, carry: float, db_path: Path = CHAIN_DB,
                 exclude_sessions: Optional[set] = None) -> dict:
    """Median (ATM IV / VIX) per DTE bucket, solved against the fitted forward."""
    obs = _atm_observations(db_path, exclude_sessions)
    vix = _vix_lookup()
    per_bucket = defaultdict(list)
    vix_seen, sessions = [], set()

    for ts, _exp, dte, strike, ce, pe, _cl, _pl, _sp, _sa in obs:
        if dte <= 0:
            continue
        s = spot_at(int(ts))
        v = _vix_at(vix, int(ts))
        if not s or not v or v <= 0:
            continue
        t_years = dte / _YEAR_DAYS
        ivs = [iv for iv in (
            implied_vol(ce, s, strike, t_years, True, carry),
            implied_vol(pe, s, strike, t_years, False, carry)) if iv]
        if not ivs:
            continue
        b = _bucket(dte)
        if b is None:
            continue
        per_bucket[b].append((dte, (sum(ivs) / len(ivs)) / v))
        vix_seen.append(v)
        sessions.add(ist_date(ts).isoformat())

    points, thin = [], []
    for b in sorted(per_bucket):
        vals = per_bucket[b]
        # x is the bucket's MEDIAN observed DTE, not its lower edge: the 0-0.5
        # bucket is really an observation at ~0.2 days, and anchoring the
        # interpolation at 0.0 would stretch the steepest part of the curve
        # across a day it was never measured over.
        entry = {
            "dte": b,
            "dte_mid": round(statistics.median(d for d, _ in vals), 3),
            "ratio": round(statistics.median(r for _, r in vals), 3),
            "n": len(vals),
        }
        if len(vals) < MIN_OBS_PER_BUCKET:
            thin.append(b)
            entry["thin"] = True
        points.append(entry)

    return {
        "points": points,
        "n_obs": sum(len(v) for v in per_bucket.values()),
        "sessions": len(sessions),
        "vix_min": round(min(vix_seen), 2) if vix_seen else None,
        "vix_max": round(max(vix_seen), 2) if vix_seen else None,
        "thin_buckets": thin,
        "caveats": [
            "Fitted on app/condor chain snapshots — the only real NIFTY option "
            "quotes on this machine. Every ratio inherits that sample's size.",
            "The sample spans one volatility regime. A ratio measured at "
            "VIX~11 is not evidence about VIX~25, where the term structure "
            "usually inverts.",
            "Snapshots are intraday only, so no bucket contains an overnight "
            "observation: the curve describes the level of IV, not the "
            "close-to-open jump in it.",
            "CE and PE IVs are averaged, so the fit carries no skew. A real "
            "ATM put trades at a small premium to the call in vol terms; that "
            "asymmetry is absent from every modelled price here.",
        ],
    }


def fit_spread(db_path: Path = CHAIN_DB) -> dict:
    """Real ATM bid-ask, the input to the study's slippage assumption.

    Reported separately for the entry window (~15:00) and the exit window
    (~09:50) because the morning book is visibly wider, and a study that pays
    the afternoon spread at both ends flatters itself.
    """
    obs = _atm_observations(db_path)
    windows = {"entry_window": (14 * 60 + 45, 15 * 60 + 15),
               "exit_window": (9 * 60 + 35, 10 * 60 + 5)}
    acc = {k: {"pct": [], "abs": []} for k in windows}
    for ts, _e, _dte, _k, _c, _p, _cl, _pl, spread_pct, spread_abs in obs:
        if spread_pct is None:
            continue
        mins = ist_minutes(ts)
        for name, (lo, hi) in windows.items():
            if lo <= mins <= hi:
                acc[name]["pct"].append(spread_pct)
                acc[name]["abs"].append(spread_abs)

    def _med(xs):
        return round(statistics.median(xs), 4) if xs else None

    return {name: {"n": len(a["pct"]), "spread_pct": _med(a["pct"]),
                   "spread_abs": _med(a["abs"])}
            for name, a in acc.items()}
