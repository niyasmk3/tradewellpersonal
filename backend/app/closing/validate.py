"""How wrong is the option model? Measured against real quotes, not asserted.

Layer 2 of this study prices options that no longer exist. That is defensible
only if the error is published, so this module scores the model three ways
against app/condor's real chain snapshots, hardest test last:

  1. LEVEL      modelled ATM premium vs the real ATM mid, by DTE. In-sample —
                the carry and vol curve were fitted on these very rows, so a
                good score here proves only that the fit is not broken. It is
                reported next to a no-calibration baseline so the fit's
                contribution is visible rather than claimed.
  2. OUT-OF-SAMPLE  leave-one-session-out: refit carry AND vol curve without
                session S, then score S. The honest level error.
  3. OVERNIGHT  the number the study actually reports — the 15:00 -> next
                09:50 percentage change in premium, modelled vs real, for the
                SAME contract (the entry strike, which is usually not the next
                morning's ATM). Level errors largely cancel in a ratio, so this
                is both the most relevant test and the most forgiving one; the
                sample is tiny and n travels with every number.

None of this makes the modelled year real. It bounds it.
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from app.closing import calibration
from app.closing.calendar import ist_date, ist_minutes
from app.closing.pricing import IvCurve, OptionModel, build_model
from app.patterns import store as patterns_store

CHAIN_DB = calibration.CHAIN_DB

_ENTRY_WINDOW = (14 * 60 + 50, 15 * 60 + 10)   # IST minutes, ~15:00
_EXIT_WINDOW = (9 * 60 + 40, 10 * 60 + 0)      # IST minutes, ~09:50
_ENTRY_TARGET, _EXIT_TARGET = 15 * 60, 9 * 60 + 50


def _spot_series() -> pd.DataFrame:
    return patterns_store.load_frame()[["ts", "open", "close"]].copy()


class SpotLookup:
    """Instantaneous index level, linearly interpolated inside its 5-min bar.

    Neither endpoint is right for a quote timestamped mid-bar: the open is up
    to 5 minutes stale and the close is up to 5 minutes in the future. The
    interpolation is used ONLY here, for scoring; the backtest itself reads
    bar boundaries and never needs it.
    """

    def __init__(self, df: pd.DataFrame) -> None:
        self._bars = {int(r.ts): (float(r.open), float(r.close)) for r in df.itertuples()}

    def at(self, ts: int) -> Optional[float]:
        base = ts - (ts % 300)
        bar = self._bars.get(base)
        if bar is None:
            for back in (300, 600):
                bar = self._bars.get(base - back)
                if bar is not None:
                    return bar[1]
            return None
        o, c = bar
        return o + (c - o) * ((ts - base) / 300.0)


def _ist_minute(ts: int) -> int:
    return ist_minutes(ts)


def _observations(spot: SpotLookup, db_path: Path = CHAIN_DB) -> list:
    """Real ATM quotes joined to real spot and real VIX."""
    obs = calibration._atm_observations(db_path)
    vix = calibration._vix_lookup()
    out = []
    for ts, expiry, dte, strike, ce, pe, _cl, _pl, _sp, _sa in obs:
        s = spot.at(int(ts))
        v = calibration._vix_at(vix, int(ts))
        if s is None or v is None or dte <= 0:
            continue
        out.append({"ts": int(ts), "session": ist_date(ts).isoformat(),
                    "expiry": expiry, "dte": dte, "strike": float(strike),
                    "ce": float(ce), "pe": float(pe), "spot": float(s), "vix": float(v)})
    return out


def _score(rows: list, model: OptionModel) -> dict:
    errs, signed = [], []
    for r in rows:
        exp = date.fromisoformat(r["expiry"])
        for is_call, real in ((True, r["ce"]), (False, r["pe"])):
            if not real or real <= 0:
                continue
            m = model.premium(r["spot"], r["strike"], r["ts"], exp, is_call, r["vix"])
            if m is None:
                continue
            errs.append(abs(m - real) / real * 100.0)
            signed.append((m - real) / real * 100.0)
    if not errs:
        return {"n": 0}
    return {
        "n": len(errs),
        "median_abs_err_pct": round(statistics.median(errs), 2),
        "mean_abs_err_pct": round(statistics.mean(errs), 2),
        "median_bias_pct": round(statistics.median(signed), 2),
        "p90_abs_err_pct": round(sorted(errs)[int(len(errs) * 0.9)], 2),
    }


def level_check(spot: SpotLookup, model: OptionModel, db_path: Path = CHAIN_DB) -> dict:
    rows = _observations(spot, db_path)
    if not rows:
        return {"available": False, "reason": "no chain snapshots on this machine"}
    by_dte = defaultdict(list)
    for r in rows:
        by_dte[calibration._bucket(r["dte"])].append(r)
    return {
        "available": True,
        "in_sample": _score(rows, model),
        # Same VIX, no carry fit and no vol term structure — what the study
        # would have reported if the calibration step had been skipped.
        "uncalibrated_baseline": _score(rows, OptionModel(IvCurve(None), None)),
        "by_dte": {str(k): _score(v, model) for k, v in sorted(by_dte.items())},
    }


def out_of_sample_check(spot: SpotLookup, db_path: Path = CHAIN_DB) -> dict:
    """Leave-one-session-out. With N sessions this is N refits; N is small."""
    rows = _observations(spot, db_path)
    sessions = sorted({r["session"] for r in rows})
    if len(sessions) < 2:
        return {"available": False,
                "reason": f"{len(sessions)} session(s) of quotes — "
                          "leave-one-out needs at least 2"}
    per_session, weighted_num, weighted_den = {}, 0.0, 0
    for s in sessions:
        held = [r for r in rows if r["session"] == s]
        if not held:
            continue
        loo_model, _ = build_model(spot.at, db_path, exclude_sessions={s})
        sc = _score(held, loo_model)
        sc["carry_pct"] = loo_model.describe()["carry_pct"]
        per_session[s] = sc
        if sc.get("n"):
            weighted_num += sc["n"] * sc["median_abs_err_pct"]
            weighted_den += sc["n"]
    return {
        "available": True,
        "sessions": len(per_session),
        "pooled_median_abs_err_pct": (round(weighted_num / weighted_den, 2)
                                      if weighted_den else None),
        "per_session": per_session,
    }


def _quotes_by_contract(db_path: Path = CHAIN_DB) -> dict:
    """(expiry, strike) -> {ts: {"CE": mid, "PE": mid}} for every logged strike.

    The overnight check cannot use ATM observations alone: the ATM strike at
    15:00 is usually NOT the ATM strike at 09:50 the next morning, and the
    trade being scored still holds the ORIGINAL strike.
    """
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT ts, expiry, strike, right, ltp, bid, ask FROM chain_snap "
            "WHERE symbol = 'NIFTY' AND ltp IS NOT NULL AND ltp > 0"
        ).fetchall()
    finally:
        conn.close()
    out: dict = defaultdict(lambda: defaultdict(dict))
    for ts, expiry, strike, right, ltp, bid, ask in rows:
        if not expiry:
            continue
        ref = (bid + ask) / 2.0 if (bid and ask and ask > bid > 0) else float(ltp)
        out[(expiry, float(strike))][int(ts)][right] = float(ref)
    return out


def overnight_check(spot: SpotLookup, model: OptionModel,
                    db_path: Path = CHAIN_DB) -> dict:
    """Modelled vs real 15:00 -> next-09:50 premium change, same contract."""
    rows = _observations(spot, db_path)
    if not rows:
        return {"available": False, "reason": "no chain snapshots on this machine"}
    contracts = _quotes_by_contract(db_path)
    vix_map = calibration._vix_lookup()

    entries: dict = {}
    for r in rows:
        m = _ist_minute(r["ts"])
        if not _ENTRY_WINDOW[0] <= m <= _ENTRY_WINDOW[1]:
            continue
        key = (r["session"], r["expiry"])
        cur = entries.get(key)
        if cur is None or abs(m - _ENTRY_TARGET) < abs(_ist_minute(cur["ts"]) - _ENTRY_TARGET):
            entries[key] = r

    sessions = sorted({r["session"] for r in rows})
    nxt = {s: sessions[i + 1] for i, s in enumerate(sessions[:-1])}

    pairs, skipped = [], []
    for (sess, expiry), e in sorted(entries.items()):
        nsess = nxt.get(sess)
        if nsess is None:
            continue
        quotes = contracts.get((expiry, e["strike"]), {})
        best_ts = None
        for ts in quotes:
            if ist_date(ts).isoformat() != nsess:
                continue
            m = _ist_minute(ts)
            if not _EXIT_WINDOW[0] <= m <= _EXIT_WINDOW[1]:
                continue
            if best_ts is None or abs(m - _EXIT_TARGET) < abs(_ist_minute(best_ts) - _EXIT_TARGET):
                best_ts = ts
        if best_ts is None:
            skipped.append({"from": sess, "expiry": expiry, "strike": e["strike"],
                            "why": "entry strike not quoted in the next 09:50 window"})
            continue
        x_spot, x_vix = spot.at(best_ts), calibration._vix_at(vix_map, best_ts)
        if x_spot is None or x_vix is None:
            skipped.append({"from": sess, "expiry": expiry, "strike": e["strike"],
                            "why": "no spot/VIX at the exit timestamp"})
            continue
        exp = date.fromisoformat(expiry)
        for is_call, tag in ((True, "CE"), (False, "PE")):
            real_in = e["ce"] if is_call else e["pe"]
            real_out = quotes[best_ts].get(tag)
            if not real_in or not real_out or real_in <= 0:
                continue
            mod_in = model.premium(e["spot"], e["strike"], e["ts"], exp, is_call, e["vix"])
            mod_out = model.premium(x_spot, e["strike"], best_ts, exp, is_call, x_vix)
            if not mod_in or mod_out is None or mod_in <= 0:
                continue
            real_chg = (real_out - real_in) / real_in * 100.0
            mod_chg = (mod_out - mod_in) / mod_in * 100.0
            pairs.append({
                "from": sess, "to": nsess, "expiry": expiry, "strike": e["strike"],
                "right": tag, "real_in": round(real_in, 2), "real_out": round(real_out, 2),
                "model_in": round(mod_in, 2), "model_out": round(mod_out, 2),
                "real_pct": round(real_chg, 1), "model_pct": round(mod_chg, 1),
                "err_pp": round(mod_chg - real_chg, 1),
            })

    if not pairs:
        return {"available": False, "skipped": skipped,
                "reason": "no contract was quoted at both 15:00 and the next 09:50"}
    errs = [abs(p["err_pp"]) for p in pairs]
    bias = [p["err_pp"] for p in pairs]
    agree = sum(1 for p in pairs if (p["real_pct"] > 0) == (p["model_pct"] > 0)) / len(pairs)
    return {
        "available": True,
        "n_pairs": len(pairs),
        "median_abs_err_pp": round(statistics.median(errs), 1),
        "median_bias_pp": round(statistics.median(bias), 1),
        "sign_agreement_pct": round(agree * 100, 1),
        "skipped": skipped,
        "pairs": sorted(pairs, key=lambda p: (p["from"], p["strike"], p["right"])),
    }


def run(spot: SpotLookup, model: OptionModel, db_path: Path = CHAIN_DB) -> dict:
    return {
        "level": level_check(spot, model, db_path),
        "out_of_sample": out_of_sample_check(spot, db_path),
        "overnight": overnight_check(spot, model, db_path),
    }
