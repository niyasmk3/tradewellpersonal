"""Orchestration between the API routes and the lab internals."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from app.config import get_settings
from app.lab import catalog, data as data_mod, learning, metrics, risk
from app.lab.engine import Backtester
from app.lab.models import validate_params
from app.lab.store import lab_store
from app.market.calendar import IST, now_ist

log = logging.getLogger("tradewell.lab.service")


class LabError(Exception):
    pass


def ensure_seeded() -> None:
    """Version 1 (catalog defaults) + state row for every strategy."""
    for d in catalog.CATALOG:
        defaults = validate_params(d.params, {})
        lab_store.ensure_version1(d.sid, defaults)


def _day_ts(day: str, end: bool = False) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
    dt = dt.replace(hour=15, minute=35) if end else dt.replace(hour=9)
    return int(dt.timestamp())


def overview() -> dict:
    cfg = get_settings()
    ready = data_mod.readiness()
    states = lab_store.all_states()
    per_status = {"ready": 0, "window_limited": 0, "awaiting_sync": 0,
                  "blocked": 0}
    for d in catalog.CATALOG:
        st = data_mod.strategy_readiness(d.data_needs, ready)["status"]
        per_status[st] = per_status.get(st, 0) + 1

    paper_days = lab_store.paper_days(limit=400)
    cum = sum(d["pnl_net"] for d in paper_days)
    today = now_ist().date().isoformat()
    today_positions = [p for p in lab_store.positions_for_day(today)]
    today_closed = [p for p in today_positions
                    if p.get("closed_ts") is not None]
    today_pnl = sum(p.get("pnl_net") or 0 for p in today_closed)
    open_pos = lab_store.open_positions()

    all_pnls = []
    curve, run_ = [], 0.0
    for d in sorted(paper_days, key=lambda x: x["day"]):
        all_pnls.append(d["pnl_net"])
        run_ += d["pnl_net"]
        curve.append({"day": d["day"], "cum": round(run_, 2)})
    wins = sum(1 for p in all_pnls if p > 0)
    gross_p = sum(p for p in all_pnls if p > 0)
    gross_l = -sum(p for p in all_pnls if p <= 0)
    dd, peak = 0.0, 0.0
    for c in curve:
        peak = max(peak, c["cum"])
        dd = min(dd, c["cum"] - peak)

    session = lab_store.paper_session(today)
    rc = risk.effective()
    exposure = 0.0
    margin_open = 0.0
    for p in open_pos:
        for l in p["legs"]:
            exposure += abs(l["price"] * l["qty"])
    return {
        "strategies_total": len(catalog.CATALOG),
        "data_readiness": per_status,
        "paper_enabled_count": sum(1 for s in states.values()
                                   if s.get("paper_enabled")),
        "experimental_count": sum(1 for s in states.values()
                                  if s.get("experimental_version")),
        "suspended_count": sum(1 for s in states.values()
                               if s.get("suspended_reason")),
        "paper_engine_on": bool(cfg.lab_paper_enabled),
        "session": {"day": today,
                    "status": session.get("status") if session else None},
        "today_pnl": round(today_pnl, 2),
        "today_trades": len(today_closed),
        "open_positions": len(open_pos),
        "cumulative_pnl": round(cum, 2),
        "paper_day_count": len(paper_days),
        "win_rate_days": round(wins / len(all_pnls) * 100, 1)
                         if all_pnls else None,
        "profit_factor_days": round(gross_p / gross_l, 2)
                              if gross_l > 0 else None,
        "max_drawdown": round(dd, 2),
        "equity_curve": curve[-120:],
        "exposure_rs": round(exposure, 0),
        "capital_rs": rc["max_capital_rs"],
        "emergency_stop": bool(rc["emergency_stop"]),
        "chain_era": {"from": ready["chain_hist"]["from"],
                      "to": ready["chain_hist"]["to"]},
        "evidence_note": "Backtest, walk-forward and paper results are "
                         "separate evidence classes and are never combined.",
    }


def library() -> list:
    ready = data_mod.readiness()
    states = lab_store.all_states()
    out = []
    runs_by_sid: dict = {}
    for r in lab_store.runs(limit=400):
        runs_by_sid.setdefault(r["sid"], []).append(r)
    paper_by_sid: dict = {}
    for d in lab_store.paper_days(limit=1000):
        b = paper_by_sid.setdefault(d["sid"], {"days": 0, "net": 0.0,
                                               "trades": 0})
        b["days"] += 1
        b["net"] = round(b["net"] + d["pnl_net"], 2)
        b["trades"] += d["trades"]
    for d in catalog.as_dicts():
        sid = d["sid"]
        st = states.get(sid, {})
        r = data_mod.strategy_readiness(tuple(d["data_needs"]), ready)
        latest_runs = {}
        for run in runs_by_sid.get(sid, []):
            k = run["kind"]
            if k not in latest_runs and run["status"] == "done":
                s = run["summary"]
                latest_runs[k] = {
                    "run_id": run["run_id"], "version": run["version"],
                    "trades": s.get("trades"), "net_pnl": s.get("net_pnl"),
                    "expectancy": s.get("expectancy"),
                    "win_rate": s.get("win_rate"),
                    "profit_factor": s.get("profit_factor"),
                    "warnings": s.get("warnings", []),
                }
        d.update({
            "readiness": r,
            "champion_version": st.get("champion_version", 1),
            "paper_enabled": bool(st.get("paper_enabled")),
            "experimental_version": st.get("experimental_version"),
            "suspended_reason": st.get("suspended_reason", ""),
            "latest_runs": latest_runs,
            "paper": paper_by_sid.get(sid),
        })
        out.append(d)
    return out


def strategy_detail(sid: str) -> dict:
    d = catalog.get(sid)
    st = lab_store.state(sid) or {}
    return {
        "sid": sid,
        "versions": lab_store.versions(sid),
        "state": st,
        "runs": lab_store.runs(sid, limit=20),
        "paper_days": lab_store.paper_days(sid, limit=60),
        "readiness": data_mod.strategy_readiness(d.data_needs),
    }


def save_version(sid: str, params: dict, note: str = "") -> dict:
    d = catalog.get(sid)
    st = lab_store.state(sid)
    champion = int(st["champion_version"]) if st else 1
    full = validate_params(d.params, params or {})
    ver = lab_store.add_version(sid, full, "user", champion, note)
    return {"sid": sid, "version": ver, "params": full}


def set_champion(sid: str, version: int) -> dict:
    if lab_store.get_params(sid, version) is None:
        raise LabError("unknown version %s for %s" % (version, sid))
    lab_store.set_state(sid, champion_version=int(version))
    return lab_store.state(sid)


def set_paper(sid: str, enabled: bool) -> dict:
    catalog.get(sid)
    lab_store.set_state(sid, paper_enabled=1 if enabled else 0,
                        suspended_reason="")
    return lab_store.state(sid)


def run_backtest(sid: str, version: Optional[int], params: Optional[dict],
                 from_day: Optional[str], to_day: Optional[str],
                 capital_rs: float, kind: str = "insample") -> dict:
    d = catalog.get(sid)
    ready = data_mod.strategy_readiness(d.data_needs)
    if ready["status"] in ("blocked", "awaiting_sync"):
        raise LabError("Insufficient historical data: %s"
                       % "; ".join(ready["notes"]))
    lo_day = max(from_day or ready["from"], ready["from"])
    hi_day = min(to_day or ready["to"], ready["to"])
    if lo_day > hi_day:
        raise LabError("requested span [%s..%s] has no usable data "
                       "(usable: %s..%s)" % (from_day, to_day,
                                             ready["from"], ready["to"]))
    if params:
        ver_info = save_version(sid, params, "backtest config")
        version = ver_info["version"]
        run_params = ver_info["params"]
    else:
        st = lab_store.state(sid)
        version = version or (int(st["champion_version"]) if st else 1)
        run_params = lab_store.get_params(sid, version)
        if run_params is None:
            raise LabError("unknown version %s" % version)
    from_ts, to_ts = _day_ts(lo_day), _day_ts(hi_day, end=True)
    data_note = ""
    if from_day and lo_day != from_day:
        data_note = "span clamped to available data (%s..%s)" % (lo_day,
                                                                 hi_day)
    # a strategy may expose "timeframe" as a param (B09); default stays the
    # catalog's fixed timeframe for everything else
    tf = run_params.get("timeframe") or d.timeframe
    run_id = lab_store.create_run(sid, version, kind, tf, from_ts,
                                  to_ts, capital_rs,
                                  learning.costs_snapshot(lo_day), data_note)
    bt = Backtester(d, run_params, tf, from_ts, to_ts, capital_rs)
    res = bt.run()
    if res.status != "done":
        lab_store.finish_run(run_id, res.status, {"note": res.note}, [])
        return {"run_id": run_id, "status": res.status, "note": res.note}
    summary = metrics.summarize(res.trades, capital_rs, res.daily_pnl,
                                res.rejects, res.equity_curve, kind,
                                res.peak_margin, res.bars)
    if data_note:
        summary.setdefault("warnings", []).append(data_note)
    lab_store.finish_run(run_id, "done", summary, res.trades)
    return {"run_id": run_id, "status": "done", "version": version,
            "summary": summary}


def paper_status() -> dict:
    today = now_ist().date().isoformat()
    open_pos = lab_store.open_positions()
    for p in open_pos:
        p["state"] = p.get("state") or {}
    return {
        "day": today,
        "session": lab_store.paper_session(today),
        "open_positions": open_pos,
        "pending_orders": lab_store.pending_orders(today),
        "signals": lab_store.signals_for_day(today, limit=100),
        "closed_today": [p for p in lab_store.positions_for_day(today)
                         if p.get("closed_ts") is not None],
        "risk": risk.effective(),
        "risk_events": lab_store.risk_events(limit=30),
    }


def data_status() -> dict:
    return {
        "readiness": data_mod.readiness(),
        "quality": data_mod.quality_report(days=14),
        "events": lab_store.data_events(limit=50),
    }


def sync_futures_now() -> dict:
    from app.services import kite_service
    from app.state import market_state
    if not kite_service.is_authenticated:
        raise LabError("Kite session required — log in first")
    meta = market_state.underlyings.get("NIFTY")
    if not meta or not meta.fut_token:
        raise LabError("NIFTY futures token not resolved yet (feed still "
                       "starting?)")
    out = data_mod.sync_futures(kite_service.kite, meta.fut_token)
    lab_store.log_data_event(int(time.time()), "fut_sync", out)
    return out
