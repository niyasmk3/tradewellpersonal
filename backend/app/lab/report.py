"""Daily learning report — assembled after each session, stored immutably
per day, rendered by the Learning Lab section of the tab."""
from __future__ import annotations

import time
from typing import Optional

from app.lab import data as data_mod
from app.lab.catalog import BY_SID
from app.lab.store import LabStore, lab_store


def build_daily(day: str, store: LabStore = lab_store) -> dict:
    session = store.paper_session(day) or {}
    signals = store.signals_for_day(day, limit=1000)
    positions = [p for p in store.positions_for_day(day)
                 if p.get("closed_ts") is not None]

    by_sid: dict = {}
    for p in positions:
        b = by_sid.setdefault(p["sid"], {"trades": 0, "net": 0.0,
                                         "wins": 0})
        b["trades"] += 1
        b["net"] += p.get("pnl_net") or 0
        if (p.get("pnl_net") or 0) > 0:
            b["wins"] += 1
    for b in by_sid.values():
        b["net"] = round(b["net"], 2)

    losers = sorted([p for p in positions if (p.get("pnl_net") or 0) < 0],
                    key=lambda p: p["pnl_net"])[:5]
    winners = sorted([p for p in positions if (p.get("pnl_net") or 0) > 0],
                     key=lambda p: -p["pnl_net"])[:5]

    def _trade_brief(p):
        return {"sid": p["sid"], "tag": p.get("tag"),
                "pnl_net": p.get("pnl_net"),
                "exit_reason": p.get("exit_reason"),
                "legs": [{"side": l["side"], "symbol": l["tradingsymbol"],
                          "entry": l["price"]} for l in p["legs"]]}

    sig_outcomes: dict = {}
    block_reasons: dict = {}
    for s in signals:
        sig_outcomes[s["outcome"]] = sig_outcomes.get(s["outcome"], 0) + 1
        if s["outcome"] == "blocked":
            block_reasons[s["reason"]] = block_reasons.get(s["reason"],
                                                           0) + 1

    exps = store.experiments(limit=50)
    exp_summary = [{
        "exp_id": e["exp_id"], "sid": e["sid"], "status": e["status"],
        "hypothesis": e["hypothesis"],
        "champion": e["champion"], "challenger": e["challenger"],
        "decision_reason": e["decision_reason"],
        "old_params": store.get_params(e["sid"], e["champion"]),
        "new_params": store.get_params(e["sid"], e["challenger"]),
        "evidence": e["evidence"],
        "test_result": {
            "challenger_expectancy": ((e.get("test_result") or {})
                                      .get("challenger") or {}
                                      ).get("expectancy"),
            "champion_expectancy": ((e.get("test_result") or {})
                                    .get("champion") or {}
                                    ).get("expectancy"),
        },
    } for e in exps if e["status"] in ("proposed", "validating", "paper")
        or (e.get("decided_at") or 0) >= _day_epoch(day)]

    suspended = [{"sid": sid, "reason": st["suspended_reason"]}
                 for sid, st in store.all_states().items()
                 if st.get("suspended_reason")]

    risk_events = [e for e in store.risk_events(limit=200)
                   if e["day"] == day]
    quality = data_mod.quality_report(days=3)

    report = {
        "day": day,
        "generated_at": int(time.time()),
        "session": {"status": session.get("status"),
                    "premarket": session.get("premarket")},
        "strategies_run": sorted({s["sid"] for s in signals})
                          or sorted(by_sid.keys()),
        "net_pnl": round(sum(p.get("pnl_net") or 0 for p in positions), 2),
        "trades": len(positions),
        "by_strategy": by_sid,
        "signal_outcomes": sig_outcomes,
        "block_reasons": block_reasons,
        "top_losers": [_trade_brief(p) for p in losers],
        "top_winners": [_trade_brief(p) for p in winners],
        "experiments": exp_summary,
        "suspended": suspended,
        "risk_events": [{"code": e["code"], "sid": e["sid"],
                         "detail": e["detail"]} for e in risk_events],
        "data_quality": {"issue_count": quality["issue_count"],
                         "issues": quality["issues"][:20]},
    }
    store.save_report(day, report)
    return report


def _day_epoch(day: str) -> int:
    from datetime import datetime
    from app.market.calendar import IST
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(tzinfo=IST).timestamp())
