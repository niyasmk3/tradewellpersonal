"""Controlled learning: champion/challenger experiments.

The point is robustness, not chasing yesterday's P&L. Hard rules:
  * A challenger is PROPOSED only from stated evidence (loss-pattern
    heuristics over enough trades), never from a good day.
  * It must beat the champion on walk-forward DEV *and* untouched TEST
    segments (rules frozen before TEST is read) to reach the experimental
    paper arm.
  * It may replace the champion only after enough independent forward
    paper evidence — and only as the PAPER champion. Live trading does not
    exist in this module, or anywhere in the Lab.
  * Every step is a row in the experiment table; every parameter set is an
    immutable version; every promotion is reversible by pointing the
    champion back.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from app.lab import data as data_mod
from app.lab import metrics
from app.lab.catalog import BY_SID
from app.lab.engine import Backtester
from app.lab.models import validate_params
from app.lab.store import LabStore, lab_store
from app.market.calendar import IST

log = logging.getLogger("tradewell.lab.learning")

MIN_TRADES_TO_PROPOSE = 30      # evidence before any hypothesis
MIN_TEST_TRADES = 10            # TEST segment observations required
MIN_PAPER_DAYS = 10             # forward paper evidence before promotion
MIN_PAPER_TRADES = 10
MAX_OPEN_EXPERIMENTS_PER_SID = 1
DEV_FRACTION = 0.7


def _day_ts(day: str, end: bool = False) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
    if end:
        d = d.replace(hour=15, minute=35)
    else:
        d = d.replace(hour=9, minute=0)
    return int(d.timestamp())


def run_span(sid: str, version: int, params: dict, from_ts: int, to_ts: int,
             kind: str, capital: float = 500000.0,
             store: LabStore = lab_store) -> Optional[dict]:
    """Run + record one backtest segment. Returns the summary or None."""
    sdef = BY_SID[sid]
    day = datetime.fromtimestamp(from_ts, IST).date().isoformat()
    run_id = store.create_run(sid, version, kind, sdef.timeframe, from_ts,
                              to_ts, capital,
                              costs_snapshot(day), data_note="")
    bt = Backtester(sdef, params, sdef.timeframe, from_ts, to_ts, capital)
    res = bt.run()
    if res.status != "done":
        store.finish_run(run_id, res.status, {"note": res.note}, [])
        return None
    summary = metrics.summarize(res.trades, capital, res.daily_pnl,
                                res.rejects, res.equity_curve, kind,
                                res.peak_margin, res.bars)
    store.finish_run(run_id, "done", summary, res.trades)
    summary["run_id"] = run_id
    return summary


def costs_snapshot(day: str) -> dict:
    from app.lab import costs
    return costs.cost_model_snapshot(day)


# --- proposal heuristics ----------------------------------------------------

def _step(spec) -> float:
    return spec.step or ((spec.hi - spec.lo) / 20.0 if
                         spec.hi is not None and spec.lo is not None else 1.0)


def _bounded(spec, value: float) -> float:
    v = value
    if spec.lo is not None:
        v = max(spec.lo, v)
    if spec.hi is not None:
        v = min(spec.hi, v)
    return int(round(v)) if spec.kind == "int" else round(v, 6)


def propose_from_evidence(sid: str, champion_summary: dict,
                          champion_params: dict) -> Optional[tuple]:
    """-> (new_params, hypothesis, evidence) or None. Reads loss patterns
    from a champion evaluation with enough trades."""
    if champion_summary.get("trades", 0) < MIN_TRADES_TO_PROPOSE:
        return None
    sdef = BY_SID[sid]
    specs = {s.key: s for s in sdef.params}
    by_exit = champion_summary.get("by_exit_reason", {})
    by_regime = champion_summary.get("by_regime", {})
    total = champion_summary["trades"]
    expectancy = champion_summary.get("expectancy") or 0.0

    def frac(reason):
        b = by_exit.get(reason, {})
        return (b.get("n", 0) / total) if total else 0.0

    # 1) stops eat the book -> widen the stop one step
    stop_key = "stop_atr" if "stop_atr" in specs else (
        "stop_pct" if "stop_pct" in specs else None)
    if stop_key and frac("stop") >= 0.45 and expectancy < 0:
        spec = specs[stop_key]
        newv = _bounded(spec, float(champion_params[stop_key])
                        + _step(spec))
        if newv != champion_params[stop_key]:
            p = dict(champion_params)
            p[stop_key] = newv
            return (p, "widen %s %s -> %s" % (stop_key,
                                              champion_params[stop_key],
                                              newv),
                    {"rule": "stop_heavy", "stop_frac": round(frac("stop"),
                                                              2),
                     "expectancy": expectancy})

    # 2) time exits bleed -> take profits nearer
    tgt_key = "target_atr" if "target_atr" in specs else (
        "target_pct" if "target_pct" in specs else None)
    sq = by_exit.get("squareoff", {})
    if tgt_key and sq.get("n", 0) >= 10 and \
            (sq.get("expectancy") or 0) < 0:
        spec = specs[tgt_key]
        newv = _bounded(spec, float(champion_params[tgt_key]) - _step(spec))
        if newv != champion_params[tgt_key]:
            p = dict(champion_params)
            p[tgt_key] = newv
            return (p, "tighten %s %s -> %s" % (tgt_key,
                                                champion_params[tgt_key],
                                                newv),
                    {"rule": "squareoff_bleed",
                     "squareoff_expectancy": sq.get("expectancy")})

    # 3) one regime destroys the book -> filter it out
    hv = by_regime.get("high_vol", {})
    if "regime_filter" in specs and \
            champion_params.get("regime_filter") == "off" and \
            hv.get("n", 0) >= 10 and (hv.get("expectancy") or 0) < 0 and \
            abs(hv.get("net", 0)) > abs(champion_summary.get("net_pnl", 0)) \
            * 0.5:
        p = dict(champion_params)
        p["regime_filter"] = "exclude_highvol"
        return (p, "exclude high-vol regime",
                {"rule": "regime_toxic", "high_vol": hv})
    return None


# --- experiment lifecycle ---------------------------------------------------

def validate_experiment(exp: dict, store: LabStore = lab_store) -> None:
    """Walk-forward: DEV on the early segment, TEST on the untouched tail.
    Rules are fixed before TEST is read — this function computes both, but
    the acceptance thresholds are these constants, not fitted values."""
    sid = exp["sid"]
    sdef = BY_SID[sid]
    ready = data_mod.strategy_readiness(sdef.data_needs)
    if ready["status"] in ("blocked", "awaiting_sync") or not ready["from"]:
        store.update_experiment(exp["exp_id"], status="rejected",
                                decided_at=int(time.time()),
                                decision_reason="data unavailable: %s"
                                % ready["notes"])
        return
    lo, hi = _day_ts(ready["from"]), _day_ts(ready["to"], end=True)
    split = lo + int((hi - lo) * DEV_FRACTION)

    champ_params = store.get_params(sid, exp["champion"])
    chall_params = store.get_params(sid, exp["challenger"])
    segs = {}
    for label, params, ver in (("champ_dev", champ_params, exp["champion"]),
                               ("chall_dev", chall_params,
                                exp["challenger"])):
        segs[label] = run_span(sid, ver, params, lo, split, "walkforward",
                               store=store)
    for label, params, ver in (("champ_test", champ_params,
                                exp["champion"]),
                               ("chall_test", chall_params,
                                exp["challenger"])):
        segs[label] = run_span(sid, ver, params, split, hi, "oos",
                               store=store)

    def _e(seg):
        return (segs.get(seg) or {}).get("expectancy")

    def _n(seg):
        return (segs.get(seg) or {}).get("trades", 0)

    dev_result = {"champion": segs.get("champ_dev"),
                  "challenger": segs.get("chall_dev")}
    test_result = {"champion": segs.get("champ_test"),
                   "challenger": segs.get("chall_test")}
    store.update_experiment(exp["exp_id"], dev_result=dev_result,
                            test_result=test_result, status="validating")

    reasons = []
    if _n("chall_test") < MIN_TEST_TRADES:
        reasons.append("TEST has %d challenger trades (<%d) — insufficient "
                       "out-of-sample evidence" % (_n("chall_test"),
                                                   MIN_TEST_TRADES))
    if _e("chall_dev") is None or _e("chall_test") is None:
        reasons.append("segment failed to produce results")
    else:
        if _e("chall_dev") <= (_e("champ_dev") if _e("champ_dev")
                               is not None else 0):
            reasons.append("no DEV improvement")
        if _e("chall_test") <= (_e("champ_test") if _e("champ_test")
                                is not None else 0):
            reasons.append("no TEST improvement (rules frozen on DEV)")
        cd = (segs.get("chall_test") or {}).get("max_drawdown", 0)
        pd_ = (segs.get("champ_test") or {}).get("max_drawdown", 0)
        if pd_ < 0 and cd < pd_ * 1.5:
            reasons.append("TEST drawdown worse than 1.5x champion")
    if reasons:
        store.update_experiment(exp["exp_id"], status="rejected",
                                decided_at=int(time.time()),
                                decision_reason="; ".join(reasons))
        return
    store.update_experiment(exp["exp_id"], status="paper")
    store.set_state(sid, experimental_version=exp["challenger"])
    log.info("experiment %s: %s v%s -> experimental paper arm",
             exp["exp_id"], sid, exp["challenger"])


def evaluate_paper_arm(exp: dict, store: LabStore = lab_store) -> None:
    """Promote or reject a challenger that has been trading the
    experimental paper arm."""
    sid = exp["sid"]
    chall_days = [d for d in store.paper_days(sid, limit=200)
                  if d["version"] == exp["challenger"]
                  and d["created_at"] >= exp["created_at"]]
    if len(chall_days) < MIN_PAPER_DAYS:
        return
    chall_trades = sum(d["trades"] for d in chall_days)
    if chall_trades < MIN_PAPER_TRADES:
        return
    chall_net = sum(d["pnl_net"] for d in chall_days)
    days_set = {d["day"] for d in chall_days}
    champ_days = [d for d in store.paper_days(sid, limit=200)
                  if d["version"] == exp["champion"] and d["day"] in days_set]
    champ_net = sum(d["pnl_net"] for d in champ_days)
    paper_result = {
        "days": len(chall_days), "challenger_net": round(chall_net, 2),
        "challenger_trades": chall_trades,
        "champion_net_same_days": round(champ_net, 2),
        "champion_days": len(champ_days),
    }
    store.update_experiment(exp["exp_id"], paper_result=paper_result)
    if chall_net > champ_net and chall_net > 0:
        store.update_experiment(
            exp["exp_id"], status="accepted", decided_at=int(time.time()),
            decision_reason="paper arm beat champion over %d shared days "
                            "(%d vs %d)" % (len(chall_days),
                                            round(chall_net),
                                            round(champ_net)))
        store.set_state(sid, champion_version=exp["challenger"],
                        experimental_version=None)
        log.info("PROMOTED (paper only): %s champion -> v%s", sid,
                 exp["challenger"])
    else:
        store.update_experiment(
            exp["exp_id"], status="rejected", decided_at=int(time.time()),
            decision_reason="paper arm did not beat champion "
                            "(%d vs %d over %d days)"
                            % (round(chall_net), round(champ_net),
                               len(chall_days)))
        store.set_state(sid, experimental_version=None)


def daily_cycle(day: str, store: LabStore = lab_store) -> dict:
    """Post-close learning pass. Returns a summary for the daily report."""
    actions = {"proposed": [], "validated": [], "paper_evaluated": [],
               "deteriorating": []}
    states = store.all_states()

    # 1) advance existing experiments
    for exp in store.experiments(limit=200):
        if exp["status"] == "proposed":
            validate_experiment(exp, store)
            actions["validated"].append(exp["exp_id"])
        elif exp["status"] == "paper":
            evaluate_paper_arm(exp, store)
            actions["paper_evaluated"].append(exp["exp_id"])

    # 2) deterioration + new proposals for paper-enabled strategies
    for sid, st in states.items():
        if not st.get("paper_enabled") or sid not in BY_SID:
            continue
        open_exps = [e for e in store.experiments(sid, limit=20)
                     if e["status"] in ("proposed", "validating", "paper")]
        recent = store.paper_days(sid, limit=10)
        if len(recent) >= 5:
            net10 = sum(d["pnl_net"] for d in recent)
            if net10 < 0:
                actions["deteriorating"].append(
                    {"sid": sid, "recent_days": len(recent),
                     "net": round(net10, 2)})
        if len(open_exps) >= MAX_OPEN_EXPERIMENTS_PER_SID:
            continue
        champ_ver = int(st["champion_version"])
        runs = [r for r in store.runs(sid, limit=10)
                if r["version"] == champ_ver and r["status"] == "done"
                and r["kind"] in ("insample", "walkforward")]
        if not runs:
            continue
        champ_summary = runs[0]["summary"]
        champ_params = store.get_params(sid, champ_ver)
        prop = propose_from_evidence(sid, champ_summary, champ_params)
        if prop is None:
            continue
        new_params, hypothesis, evidence = prop
        sdef = BY_SID[sid]
        try:
            new_params = validate_params(sdef.params, new_params)
        except ValueError as exc:
            log.warning("proposal for %s failed validation: %s", sid, exc)
            continue
        ver = store.add_version(sid, new_params, "learning", champ_ver,
                                hypothesis)
        exp_id = store.create_experiment(sid, champ_ver, ver, hypothesis,
                                         evidence)
        actions["proposed"].append({"sid": sid, "exp_id": exp_id,
                                    "hypothesis": hypothesis})
        log.info("proposed challenger %s v%s: %s", sid, ver, hypothesis)
    return actions
