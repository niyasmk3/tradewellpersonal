#!/usr/bin/env python3
"""Exit twins — two pre-registered counterfactual EXIT ledgers, paired 1:1
with the paper book's real fills (PROGRAM.md "Hypotheses from the pro-trader
interview", queued 10-Oct-2026, running as twins since 10-Oct-2026).

Each clean (non-hollow) paper fill gets two invisible twins that share its
entry, contract, size and charges and differ ONLY in the exit:

  A  "blast exit"  — the trader's "the spike IS the exit": sell at the first
                     60-second chain snapshot after entry whose premium is up
                     >= BLAST_PCT versus the PREVIOUS snapshot; otherwise the
                     twin follows the book's real exit.
  B  "time-stop"   — intraday and positional only: sell at the first snapshot
                     that is >= TIME_STOP_MIN in-session minutes after the LAST
                     new premium high (high-water mark seeded at the entry
                     fill); otherwise the book's real exit.

The premium path is replayed from the recorder's 60-second chain snapshots
(recorder/data/tradewell_history.db) with the same payload parsing as
dataset.premium_momentum (dataset.decompress + the ce_ltp/pe_ltp row keys);
the contract is matched by INSTRUMENT TOKEN, because the recorder stores the
front-week chain and strike-only matching would silently read the weekly
contract for a monthly-expiry positional (those fills are declared "not
gradeable" instead). A path is gradeable only if the contract is present in
the recorded chain and no in-session gap > GAP_S exists anywhere from entry to
the book's exit (entry -> first snapshot, consecutive snapshots, last snapshot
-> exit). Policy A's first step is measured from the ENTRY FILL — the premium
the buyer actually holds — and later steps snapshot-to-snapshot, so a spike
that happened before the fill is never credited to the twin (the first run on
10-Oct showed a twin "blasting" seconds after entry at a price below its own
fill when the pre-entry anchor was used as the first baseline).

Charges: BOTH arms are netted identically with the paper book's own Zerodha
schedule (backend/app/paper/charges.py, 2 legs — the function the upstream
R&D policy twins use in rnd/policies.py::_net) on the slippage-adjusted fill.
The twin's fill mirrors the book's simulated exit fill rule: LTP x (1 -
SLIPPAGE_PCT), 2dp, min Rs0.05. The paired delta is twin net - real net per
trade; R uses dataset.initial_stop() (the dataset's net_R convention).
NOTE: the book's stored realized_pnl — dataset.py's net_pnl label — equals
(exit - entry) x qty on every clean fill, i.e. it is slippage-adjusted but
GROSS of charges; re-netting both arms here keeps the delta charge-consistent.

House rule: the verdict text is literally "pending — N/30 diverged pairs"
until 30 diverged pairs exist for that policy AND mode. Nothing here changes
a live rule; the ledger is evidence for a human to read.

Run (nightly, right after dataset.py in evening_report.sh):
    backend/.venv/bin/python recorder/exit_twins.py
    backend/.venv/bin/python recorder/exit_twins.py --calibrate
        prints the |one-minute move| distribution over all gradeable paths
        WITHOUT computing a single twin outcome — the pre-registration step
        that froze BLAST_PCT on 10-Oct-2026.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dataset as ds  # noqa: E402 — the dataset builder's loaders, parsing and R convention

ROOT = HERE.parent
BACKEND, DATA, DB, IST = ds.BACKEND, ds.DATA, ds.DB, ds.IST
OUT = DATA / "exit_twins.json"

# ---------------------------------------------------------------------------
# FROZEN SPECS (10-Oct-2026). Changing any value resets the verdict clock
# (setup-detector rule) and must be written into experiment/PROGRAM.md first.
# ---------------------------------------------------------------------------
FROZEN_ON = "2026-10-10"
BLAST_PCT: float | None = 5.0    # frozen 10-Oct-2026 by --calibrate: p95 of |1-min moves| = 5.412% over 836 unique
                                 # steps -> 5% (a first pass that anchored the path one snapshot BEFORE entry read 5.924%
                                 # -> 6%; those entry-straddling steps were the flaw fixed the same day, see PROGRAM.md)
TIME_STOP_MIN = 90               # the lower edge of the trader's "1.5-2h"
TIME_STOP_MODES = ("intraday", "positional")
GAP_S = 180                      # max in-session gap between path samples
SLIPPAGE_PCT = 0.004             # the book's PAPER_SLIPPAGE_PCT default; fills verified against it
MIN_DIVERGED = 30                # house rule, per policy per mode
SESSION_OPEN_MIN, SESSION_CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30   # NSE F&O, IST
MODES = ("scalp", "intraday", "positional")

POLICIES = {
    "blast": "A · blast exit — sell on the first one-minute premium jump ≥ X% in your favour",
    "time_stop": "B · time-stop — sell once 90 in-session minutes pass without a new premium high",
}


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Charges: the book's own schedule, imported read-only by file path (no app
# package import, no side effects). Fallback mirror if the file ever moves.
# ---------------------------------------------------------------------------

def _charges_fallback(entry: float, exit_premium: float, qty: int, legs: int = 2) -> float:
    """Mirror of backend/app/paper/charges.py as of 10-Oct-2026."""
    if qty <= 0 or entry <= 0:
        return 0.0
    buy, sell = entry * qty, (exit_premium * qty if legs == 2 else 0.0)
    turnover = buy + sell
    brokerage = 20.0 * legs
    stt = sell * 0.0015
    exch, ipft, sebi = turnover * 0.0003503, turnover * 0.000005, turnover * 0.000001
    gst = (brokerage + exch + ipft + sebi) * 0.18
    stamp = buy * 0.00003
    return round(brokerage + stt + exch + ipft + sebi + gst + stamp, 2)


def load_charges():
    p = BACKEND / "app" / "paper" / "charges.py"
    try:
        spec = importlib.util.spec_from_file_location("tw_paper_charges", p)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        fn = mod.charges
        # Pin the worked example the backend's own tests pin; drift = fallback.
        if abs(fn(100.0, 110.0, 65) - _charges_fallback(100.0, 110.0, 65)) > 0.02:
            log("WARN exit_twins: backend charge schedule differs from the 10-Oct mirror "
                "— using the BACKEND schedule (the book's truth); re-check PROGRAM.md note")
        return fn, "backend/app/paper/charges.py (read-only import)"
    except Exception as exc:  # file moved / unreadable: fail-soft, loudly
        log(f"WARN exit_twins: could not import backend charges ({exc!r}); "
            "using the frozen 10-Oct-2026 mirror")
        return _charges_fallback, "mirror of backend/app/paper/charges.py frozen 10-Oct-2026"


# ---------------------------------------------------------------------------
# Session calendar + in-session time
# ---------------------------------------------------------------------------

def session_days(db: sqlite3.Connection) -> set[date]:
    """Trading days = weekdays the recorder saw a NIFTY 3m candle or a chain
    snapshot. A weekday with neither is a holiday for gap purposes; a weekday
    with candles but no chain snapshots is a recorder outage and stays a gap."""
    days: set[date] = set()
    if ds._table_exists(db, "candles"):
        for (bar_ts,) in db.execute(
            "SELECT DISTINCT (bar_ts/86400)*86400 FROM candles WHERE tf='3m' "
            "AND symbol IN ('NIFTY','NIFTY_SPOT')"):
            days.add(datetime.fromtimestamp(bar_ts, IST).date())
    if ds._table_exists(db, "snapshots"):
        for (day,) in db.execute(
            "SELECT DISTINCT substr(ts_ist,1,10) FROM snapshots WHERE endpoint='chain'"):
            try:
                days.add(date.fromisoformat(day))
            except ValueError:
                pass
    return {d for d in days if d.weekday() < 5}


def in_session_seconds(a: int, b: int, days: set[date]) -> int:
    """Seconds of NSE session (09:15-15:30 IST on trading days) between a and b."""
    if b <= a:
        return 0
    total = 0.0
    d = datetime.fromtimestamp(a, IST).date()
    last = datetime.fromtimestamp(b, IST).date()
    while d <= last:
        if d in days:
            o = datetime(d.year, d.month, d.day, SESSION_OPEN_MIN // 60, SESSION_OPEN_MIN % 60, tzinfo=IST).timestamp()
            c = datetime(d.year, d.month, d.day, SESSION_CLOSE_MIN // 60, SESSION_CLOSE_MIN % 60, tzinfo=IST).timestamp()
            lo, hi = max(a, o), min(b, c)
            if hi > lo:
                total += hi - lo
        d += timedelta(days=1)
    return int(round(total))


def ist_day(ts: int) -> date:
    return datetime.fromtimestamp(ts, IST).date()


# ---------------------------------------------------------------------------
# Premium path from the 60-second chain snapshots
# ---------------------------------------------------------------------------

def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def contract_ltp(chain: dict | None, token, strike, direction: str, expiry) -> float | None:
    """Same row/key convention as dataset.premium_momentum, matched by token
    (fallback: strike + identical chain expiry)."""
    if not isinstance(chain, dict):
        return None
    tok_key = "ce_token" if direction == "CE" else "pe_token"
    ltp_key = "ce_ltp" if direction == "CE" else "pe_ltp"
    rows = [r for r in chain.get("rows") or [] if isinstance(r, dict)]
    row = next((r for r in rows if token is not None and r.get(tok_key) == token), None)
    if row is None and chain.get("expiry") == expiry:
        row = next((r for r in rows if r.get("strike") == strike), None)
    if row is None:
        return None
    v = row.get(ltp_key)
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def build_path(db: sqlite3.Connection, trade: dict, days: set[date]) -> dict:
    """Returns {"ok": True, "samples": [(ts, ltp), ...]} — the contract's
    premium at every chain snapshot strictly after entry and up to the book's
    exit — or {"ok": False, "reason": ..., "detail": ...}. The query window
    starts GAP_S before entry only so the contract's presence in the chain can
    be established for very short holds."""
    ent, ex = int(trade["entered_at"]), int(trade["exited_at"])
    rows = db.execute(
        "SELECT ts_utc, payload FROM snapshots WHERE endpoint='chain' AND symbol=? "
        "AND ts_utc>=? AND ts_utc<=? ORDER BY ts_utc",
        (trade.get("symbol"), _iso_utc(ent - GAP_S), _iso_utc(ex)),
    ).fetchall()
    if not rows:
        return {"ok": False, "reason": "no chain snapshots in holding window"}
    raw: list[tuple[int, float | None]] = []
    for ts_utc, blob in rows:
        try:
            ts = int(datetime.fromisoformat(ts_utc).timestamp())
        except ValueError:
            continue
        chain = ds.decompress(blob)
        raw.append((ts, contract_ltp(chain, trade.get("token"), trade.get("strike"),
                                     str(trade.get("direction")), trade.get("expiry"))))
    present = [(ts, p) for ts, p in raw if p is not None]
    if not present:
        return {"ok": False,
                "reason": "contract not in recorded chain (front-week chain only)",
                "detail": f"trade expiry {trade.get('expiry')}"}
    samples = [(ts, p) for ts, p in present if ts > ent]
    # Gap rule — in-session seconds from entry to the first sample, between
    # consecutive samples, and from the last sample to the book's exit.
    # Snapshots that exist but lack the contract row mean the strike drifted
    # out of the recorded window.
    checkpoints = [(ent, None)] + samples + [(ex, None)]
    for (t0, _), (t1, _) in zip(checkpoints, checkpoints[1:]):
        gap = in_session_seconds(t0, t1, days)
        if gap > GAP_S:
            missing_rows = any(t0 < ts < t1 and p is None for ts, p in raw)
            reason = ("contract left the recorded strike window" if missing_rows
                      else "gap > 3 min inside holding window")
            return {"ok": False, "reason": reason,
                    "detail": f"{gap // 60} min gap from {datetime.fromtimestamp(t0, IST):%d-%b %H:%M}"}
    return {"ok": True, "samples": samples}


def one_minute_steps(samples: list[tuple[int, float]]) -> list[tuple[int, int, float, float]]:
    """(ts_prev, ts, p_prev, p) for consecutive samples on the same session day."""
    out = []
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        if ist_day(t0) == ist_day(t1) and p0 > 0:
            out.append((t0, t1, p0, p1))
    return out


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

def twin_fill(ltp: float) -> float:
    """The book's simulated exit-fill rule (paper/service.py): slippage against us."""
    return round(max(0.05, ltp * (1 - SLIPPAGE_PCT)), 2)


def blast_exit(samples, entry_premium: float, entered_at: int, exited_at: int, x_pct: float):
    """First snapshot after entry whose premium is >= x_pct above the previous
    sample. The first sample's baseline is the entry fill; a step that crosses
    a session boundary (overnight) is not a one-minute jump and never fires."""
    prev_ts, prev_p = entered_at, float(entry_premium)
    for ts, p in samples:
        if ts <= entered_at or ts >= exited_at:
            continue
        if prev_p > 0 and ist_day(prev_ts) == ist_day(ts) and (p - prev_p) / prev_p * 100.0 >= x_pct:
            return ts, p
        prev_ts, prev_p = ts, p
    return None


def time_stop_exit(samples, entry_premium: float, entered_at: int, exited_at: int,
                   days: set[date], minutes: int):
    hwm, t_hwm = float(entry_premium), entered_at
    for ts, p in samples:
        if ts <= entered_at or ts >= exited_at:
            continue
        if p > hwm:
            hwm, t_hwm = p, ts
            continue
        if in_session_seconds(t_hwm, ts, days) >= minutes * 60:
            return ts, p
    return None


# ---------------------------------------------------------------------------
# Pairing + summary
# ---------------------------------------------------------------------------

def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 3)


def summarize(pairs: list[dict]) -> dict:
    div = [p for p in pairs if p["diverged"]]
    d_rs = [p["delta_rs"] for p in div]
    d_r = [p["delta_R"] for p in div if p.get("delta_R") is not None]
    out = {
        "pairs": len(pairs),
        "diverged": len(div),
        "diverged_economic": sum(1 for p in div if abs(p["delta_rs"]) > 1.0),
        "twin_better": sum(1 for p in div if p["delta_rs"] > 0),
        "twin_worse": sum(1 for p in div if p["delta_rs"] < 0),
        "twin_better_share_diverged": round(sum(1 for p in div if p["delta_rs"] > 0) / len(div), 3) if div else None,
        "mean_delta_rs": round(statistics.fmean(d_rs), 1) if d_rs else None,
        "median_delta_rs": round(statistics.median(d_rs), 1) if d_rs else None,
        "mean_delta_R": round(statistics.fmean(d_r), 3) if d_r else None,
        "median_delta_R": round(statistics.median(d_r), 3) if d_r else None,
        "r_graded": len(d_r),
        "mean_delta_rs_all_pairs": round(statistics.fmean(p["delta_rs"] for p in pairs), 1) if pairs else None,
        "real_total_net_rs": round(sum(p["real_net"] for p in pairs), 1),
        "twin_total_net_rs": round(sum(p["twin_net"] for p in pairs), 1),
    }
    if len(div) < MIN_DIVERGED or out["mean_delta_rs"] is None:
        out["verdict"] = f"pending — {len(div)}/{MIN_DIVERGED} diverged pairs"
    else:
        m = out["mean_delta_rs"]
        word = "BEATS" if m > 0 else ("LOSES TO" if m < 0 else "TIES")
        out["verdict"] = (f"twin {word} the book by ₹{abs(m):,.0f} per diverged pair "
                          f"({len(div)} diverged pairs, {out['twin_better']} better)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--calibrate", action="store_true",
                    help="print the |one-minute move| distribution over gradeable paths "
                         "and stop — no twin outcome is computed")
    args = ap.parse_args(argv)

    if not DB.exists():
        log("exit_twins: recorder DB missing — nothing to replay")
        return 0
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    days = session_days(db)
    cards = ds.load_jsonl_final(BACKEND / ".signals_archive.jsonl")
    paper = ds.load_trades(BACKEND / ".paper_trades.json")
    charges, charges_source = load_charges()

    clean = [t for t in paper if not str(t.get("notes") or "").startswith("hollow")]
    not_gradeable: dict[str, dict[str, int]] = {}      # reason -> mode -> n
    ng_details: list[dict] = []
    paths: list[tuple[dict, list]] = []

    def mark(t: dict, reason: str, detail: str = "") -> None:
        not_gradeable.setdefault(reason, {}).setdefault(t.get("mode", "?"), 0)
        not_gradeable[reason][t.get("mode", "?")] += 1
        ng_details.append({"id": t.get("id"), "contract": t.get("contract"), "mode": t.get("mode"),
                           "entered": ds.datetime.fromtimestamp(t["entered_at"], IST).strftime("%d-%b %H:%M")
                           if isinstance(t.get("entered_at"), (int, float)) else None,
                           "reason": reason, "detail": detail})

    for t in clean:
        if str(t.get("status", "")).lower() != "exited" or t.get("exit_premium") is None:
            continue                                   # still open: no pair yet
        if not all(isinstance(t.get(k), (int, float)) for k in
                   ("entered_at", "exited_at", "entry_premium", "exit_premium", "realized_pnl")):
            mark(t, "missing entry/exit stamp or premium")
            continue
        if t.get("id") in ds._EXCLUDED_IDS or ds._off_session_exit(t):
            mark(t, "quarantined (off-session exit or journal override)")
            continue
        path = build_path(db, t, days)
        if not path["ok"]:
            mark(t, path["reason"], path.get("detail", ""))
            continue
        paths.append((t, path["samples"]))

    # --- the pre-registration distribution: |one-minute moves| over all gradeable paths
    seen: set[tuple] = set()
    moves: list[float] = []
    for t, samples in paths:
        for t0, t1, p0, p1 in one_minute_steps(samples):
            key = (t.get("token"), t0, t1)
            if key in seen:
                continue
            seen.add(key)
            moves.append(abs(p1 - p0) / p0 * 100.0)
    dist = {f"p{int(q * 100)}": percentile(moves, q) for q in (0.5, 0.75, 0.9, 0.95, 0.99)}
    dist["max"] = round(max(moves), 3) if moves else None
    dist["unique_one_minute_steps"] = len(moves)

    n_ng = sum(sum(m.values()) for m in not_gradeable.values())
    log(f"exit twins: clean fills {len(clean)} | gradeable paths {len(paths)} | "
        f"not gradeable {n_ng} "
        + "(" + ", ".join(f"{r}: {sum(m.values())}" for r, m in
                          sorted(not_gradeable.items(), key=lambda kv: -sum(kv[1].values()))) + ")")
    log(f"|one-minute premium move| over {len(moves):,} unique steps: "
        + " · ".join(f"{k} {v}%" for k, v in dist.items() if k.startswith("p")))
    if args.calibrate:
        p95 = dist.get("p95")
        log(f"CALIBRATION ONLY — no twin outcome computed. p95 = {p95}% → "
            f"freeze BLAST_PCT = {max(1, round(p95)) if p95 is not None else '?'} (whole percent) "
            f"in this file and experiment/PROGRAM.md, dated today.")
        return 0
    if BLAST_PCT is None:
        log("exit_twins: BLAST_PCT is not frozen — run --calibrate first, write X into "
            "PROGRAM.md, then set BLAST_PCT. No outcome computed.")
        return 2

    # --- the twins
    results: dict[str, dict] = {k: {"desc": v, "modes": {}, "diverged_pairs": []} for k, v in POLICIES.items()}
    buckets: dict[str, dict[str, list[dict]]] = {k: {m: [] for m in MODES} for k in POLICIES}
    for t, samples in paths:
        mode = str(t.get("mode"))
        if mode not in MODES:
            continue
        entry, real_exit = float(t["entry_premium"]), float(t["exit_premium"])
        qty = int(t.get("initial_quantity") or t.get("quantity") or 0)
        ent, ex = int(t["entered_at"]), int(t["exited_at"])
        real_net = round(float(t["realized_pnl"]) - charges(entry, real_exit, qty, 2), 2)
        stop = ds.initial_stop(t, cards.get(str(t.get("signal_id"))))
        r_denom = (entry - stop) * qty if isinstance(stop, (int, float)) and entry > stop and qty > 0 else None

        hits = {"blast": blast_exit(samples, entry, ent, ex, BLAST_PCT)}
        if mode in TIME_STOP_MODES:
            hits["time_stop"] = time_stop_exit(samples, entry, ent, ex, days, TIME_STOP_MIN)
        for pol, hit in hits.items():
            if hit is None:
                twin_ts, fill, twin_net = ex, real_exit, real_net
            else:
                twin_ts, ltp = hit
                fill = twin_fill(ltp)
                twin_net = round((fill - entry) * qty - charges(entry, fill, qty, 2), 2)
            pair = {
                "id": t.get("id"), "signal_id": t.get("signal_id"), "contract": t.get("contract"),
                "mode": mode, "entered_at": ent, "exited_at": ex,
                "real_exit_reason": t.get("auto_close_reason") or t.get("exit_reason"),
                "entry": entry, "real_fill": real_exit, "twin_fill": fill,
                "twin_exit_at": twin_ts, "diverged": hit is not None,
                "real_net": real_net, "twin_net": twin_net,
                "delta_rs": round(twin_net - real_net, 2),
                "delta_R": round((twin_net - real_net) / r_denom, 4) if r_denom else None,
                "hold_min_real": round((ex - ent) / 60, 1),
                "hold_min_twin": round((twin_ts - ent) / 60, 1),
            }
            buckets[pol][mode].append(pair)
            if pair["diverged"]:
                results[pol]["diverged_pairs"].append(pair)

    for pol in POLICIES:
        all_pairs: list[dict] = []
        for mode in MODES:
            if pol == "time_stop" and mode not in TIME_STOP_MODES:
                continue
            results[pol]["modes"][mode] = summarize(buckets[pol][mode])
            all_pairs += buckets[pol][mode]
        results[pol]["modes"]["all"] = summarize(all_pairs)
        results[pol]["not_gradeable"] = {
            r: {m: n for m, n in by_mode.items()
                if pol != "time_stop" or m in TIME_STOP_MODES}
            for r, by_mode in not_gradeable.items()}
        results[pol]["not_gradeable"] = {r: v for r, v in results[pol]["not_gradeable"].items() if v}
        results[pol]["diverged_pairs"].sort(key=lambda p: p["entered_at"])
        if pol == "time_stop":
            results[pol]["modes_out_of_scope"] = [m for m in MODES if m not in TIME_STOP_MODES]

    out = {
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "frozen": {
            "frozen_on": FROZEN_ON,
            "blast_pct": BLAST_PCT,
            "blast_rule": "p95 of |one-minute premium move| (snapshot-to-snapshot, same session day) across all gradeable paths, rounded to a whole percent, chosen before any twin outcome was computed; first step after entry is measured from the entry fill",
            "blast_p95_recomputed_today": dist.get("p95"),
            "time_stop_minutes": TIME_STOP_MIN,
            "time_stop_clock": "in-session minutes since the last new premium high (high-water mark seeded at the entry fill)",
            "time_stop_modes": list(TIME_STOP_MODES),
            "gap_rule_minutes": GAP_S // 60,
            "slippage_pct": SLIPPAGE_PCT,
            "charges": ("both arms: (fill - entry) x qty - Zerodha round-trip charges on the slippage-adjusted fill; "
                        f"schedule source: {charges_source}"),
            "r_convention": "delta / ((entry - initial_stop) x qty), dataset.initial_stop()",
            "divergence": "the twin exited on its own trigger before the book's exit (twin exit time != real exit time); "
                          "diverged_economic additionally requires |delta| > Rs1 like the upstream R&D twins",
            "min_diverged": MIN_DIVERGED,
        },
        "paths": {
            "clean_fills": len(clean),
            "gradeable": len(paths),
            "not_gradeable_total": n_ng,
            "not_gradeable": not_gradeable,
            "not_gradeable_detail": ng_details,
            "abs_one_minute_move_pct": dist,
            "session_days_known": len(days),
        },
        "policies": results,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False))

    log(f"frozen ({FROZEN_ON}): blast X = {BLAST_PCT}% (p95 today {dist.get('p95')}%) · "
        f"time-stop {TIME_STOP_MIN} min on {', '.join(TIME_STOP_MODES)} · slippage {SLIPPAGE_PCT:.1%} · "
        f"charges both arms via {charges_source}")
    for pol in POLICIES:
        for mode, s in results[pol]["modes"].items():
            mean = f"₹{s['mean_delta_rs']:+,.0f}" if s["mean_delta_rs"] is not None else "—"
            med = f"₹{s['median_delta_rs']:+,.0f}" if s["median_delta_rs"] is not None else "—"
            r = f"{s['mean_delta_R']:+.2f}R" if s["mean_delta_R"] is not None else "—"
            better = (f"{s['twin_better']}/{s['diverged']}" if s["diverged"] else "—")
            log(f"  {pol:9s} {mode:10s} pairs {s['pairs']:3d} diverged {s['diverged']:3d} "
                f"twin better {better:>5s} mean Δ {mean:>8s} median Δ {med:>8s} ({r}) — {s['verdict']}")
    log(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
