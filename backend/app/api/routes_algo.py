"""Algo module REST — status for the tab, and the four human actions.

AUTH, BECAUSE THIS ROUTE CAN ARM A TRADING ENGINE. The rest of the API has no
authentication and is loopback-bound precisely because it only exposes
readable data. `require_control_token` gates every mutating route here with
ALGO_CONTROL_TOKEN from .env; an empty token refuses rather than allows.

KILL IS THE EXCEPTION. It needs no token: the one request that must always
work is the one that stops the runner, and the worst an unauthenticated kill
can do is halt trading. CLEAR (un-killing) is gated like an arm.

ARMING IS TYPED, NOT CLICKED. The body must carry the exact confirmation
phrase "ARM <STRATEGY> <MODE>" — a click on the wrong button must not be
enough. Only the dry-run mode is reachable today: paper (build step 8) and
live (step 10, not authorised by the plan) return 409 by construction.
"""
from __future__ import annotations

import hmac
import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app.algo import contract
from app.algo.adapters import closing as closing_adapter
from app.algo.arm import arm_store
from app.algo.guard import check_names
from app.algo.killswitch import kill_switch
from app.algo.ledger import algo_ledger
from app.algo.runner import ADAPTERS, live_world
from app.config import get_settings
from app.market.calendar import IST

log = logging.getLogger("tradewell.algo")

router = APIRouter(prefix="/algo", tags=["algo"])

REACHABLE_MODES = ("dry",)              # widens at build steps 8 and 10


def require_control_token(x_algo_token: Optional[str] = Header(default=None)) -> None:
    expected = get_settings().algo_control_token
    if not expected:
        raise HTTPException(status_code=403, detail=(
            "ALGO_CONTROL_TOKEN is not set in .env — mutating /algo routes refuse "
            "until it is (see .env.example)"))
    if not x_algo_token or not hmac.compare_digest(
            x_algo_token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="bad or missing X-Algo-Token")


class ArmRequest(BaseModel):
    strategy: str
    mode: str
    confirm: str                        # must equal "ARM <STRATEGY> <MODE>"
    by: str = "ui"


class ReasonRequest(BaseModel):
    reason: str = ""
    by: str = "ui"


def _contract_view() -> dict:
    return {
        "freeze_date": contract.FREEZE_DATE.isoformat(),
        "max_orders_per_sec": contract.MAX_ORDERS_PER_SEC,
        "max_orders_per_day": contract.MAX_ORDERS_PER_DAY,
        "exit_reserve_orders": contract.EXIT_RESERVE_ORDERS,
        "max_open_orders_per_day": contract.MAX_OPEN_ORDERS_PER_DAY,
        "max_open_positions": contract.MAX_OPEN_POSITIONS,
        "max_lots_per_order": contract.MAX_LOTS_PER_ORDER,
        "max_notional_per_order_rs": contract.MAX_NOTIONAL_PER_ORDER_RS,
        "daily_loss_cap_rs": contract.DAILY_LOSS_CAP_RS,
        "unset_live_caps": list(contract.unset_live_caps()),
        "allowed_segments": sorted(contract.ALLOWED_SEGMENTS),
        "allowed_order_types": sorted(contract.ALLOWED_ORDER_TYPES),
        "entry_window_min": list(contract.ENTRY_WINDOW_MIN),
        "hard_flatten_min": contract.HARD_FLATTEN_MIN,
        "cas_freeze_min": list(contract.CAS_FREEZE_MIN),
        "max_tick_age_s": contract.MAX_TICK_AGE_S,
        "max_clock_skew_s": contract.MAX_CLOCK_SKEW_S,
        "max_reconcile_age_s": contract.MAX_RECONCILE_AGE_S,
        "arm_ttl_s": contract.ARM_TTL_S,
        "consecutive_rejects_kill": contract.CONSECUTIVE_REJECTS_KILL,
        "modes": list(contract.MODES),
        "reachable_modes": list(REACHABLE_MODES),
    }


def _adapter_view(today) -> dict:
    card = closing_adapter.logged_card(today)
    go, why = closing_adapter.entry_decision(card)
    return {
        "closing": {
            "entry_policy": closing_adapter.ENTRY_POLICY,
            "entry_window_min": list(closing_adapter.ENTRY_WINDOW_MIN),
            "exit_window_min": list(closing_adapter.EXIT_WINDOW_MIN),
            "lots": closing_adapter.LOTS,
            "product": closing_adapter.PRODUCT,
            "limit_buffer_pct": closing_adapter.LIMIT_BUFFER_PCT,
            "card_logged": bool(card),
            "card": ({"date": card["date"], "as_of": card["as_of"],
                      "verdict": card["verdict"], "red": card.get("red") or [],
                      "direction": card["values"].get("direction"),
                      "strike": card["values"].get("strike"),
                      "expiry": card["values"].get("expiry"),
                      "next_trading_day": card["values"].get("next_trading_day")}
                     if card else None),
            "would_fire": go,
            "decision": why,
        }
    }


@router.get("/status")
def status() -> dict:
    now = datetime.now(IST)
    now_ts = now.timestamp()
    day = now.date().isoformat()
    arm = arm_store.current(now=now_ts)
    world = live_world(now)
    ds = algo_ledger.day_state(day)
    positions = algo_ledger.open_position_keys()
    cfg = get_settings()
    preflight = [
        {"key": "control_token", "ok": bool(cfg.algo_control_token),
         "detail": "ALGO_CONTROL_TOKEN set" if cfg.algo_control_token
         else "ALGO_CONTROL_TOKEN missing in .env — arm/clear refuse"},
        {"key": "kill_switch", "ok": not kill_switch.tripped,
         "detail": "clear" if not kill_switch.tripped
         else "TRIPPED: %s" % kill_switch.code},
        {"key": "trading_day", "ok": world["trading_day"],
         "detail": "trading day" if world["trading_day"] else "not a trading day"},
        {"key": "token", "ok": world["token_state"] == "valid",
         "detail": "Kite token %s" % world["token_state"]},
        {"key": "feed", "ok": world["feed_healthy"],
         "detail": "feed healthy" if world["feed_healthy"] else "feed not healthy"},
        {"key": "tick_age", "ok": (world["last_tick_age_s"] is not None
                                   and world["last_tick_age_s"] <= contract.MAX_TICK_AGE_S),
         "detail": "last tick %ss ago" % world["last_tick_age_s"]
         if world["last_tick_age_s"] is not None else "no tick this process"},
        {"key": "clock", "ok": (world["clock_skew_s"] is not None
                                and abs(world["clock_skew_s"]) <= contract.MAX_CLOCK_SKEW_S),
         "detail": "skew %ss vs exchange" % world["clock_skew_s"]
         if world["clock_skew_s"] is not None
         else ("skew unknown (no tick yet)" if world.get("market_open")
               else "skew unmeasurable — market closed")},
        {"key": "static_ip", "ok": None,
         "detail": "not checked — required for LIVE only (build step 10)"},
        {"key": "live_caps", "ok": not contract.unset_live_caps(),
         "detail": "set" if not contract.unset_live_caps()
         else "unset (LIVE blocked): " + ", ".join(contract.unset_live_caps())},
    ]
    return {
        "now": now.isoformat(timespec="seconds"),
        "day": day,
        "ist_minute": now.hour * 60 + now.minute,
        "arm": ({"strategy": arm.strategy, "mode": arm.mode, "by": arm.by,
                 "armed_at": arm.armed_at, "expires_at": arm.expires_at,
                 "remaining_s": round(arm.remaining_s(now_ts))} if arm else None),
        "kill": kill_switch.state(),
        "preflight": preflight,
        "world": world,
        "today": {"orders": ds.orders_today, "open_orders": ds.open_orders_today,
                  "spent_keys": sorted(ds.seen_keys)},
        "positions": positions,
        "contract": _contract_view(),
        "adapters": _adapter_view(now.date()),
        "strategies": list(ADAPTERS),
        "checks": [{"code": c, "scope": s} for c, s in check_names()],
        "note": ("Only the dry-run broker exists. Nothing in this process can place "
                 "an order; every intent is gated, logged and acknowledged "
                 "synthetically. Plan: docs/algo-tab-plan-2026-09-16.md"),
    }


@router.get("/ledger")
def ledger(day: Optional[str] = Query(default=None),
           n: int = Query(default=200, ge=1, le=2000)) -> dict:
    rows = algo_ledger.tail(n, day)
    return {"rows": list(reversed(rows)), "count": len(rows), "day": day}


@router.post("/arm", dependencies=[Depends(require_control_token)])
def arm(req: ArmRequest) -> dict:
    strategy, mode = req.strategy.strip().lower(), req.mode.strip().lower()
    if strategy not in ADAPTERS:
        raise HTTPException(status_code=422, detail="unknown strategy %r; known: %s" % (
            strategy, ", ".join(ADAPTERS)))
    if mode not in contract.MODES:
        raise HTTPException(status_code=422, detail="unknown mode %r" % mode)
    if mode not in REACHABLE_MODES:
        raise HTTPException(status_code=409, detail=(
            "mode %r is not reachable yet — paper lands at build step 8, live at "
            "step 10 and is not authorised by the plan" % mode))
    phrase = "ARM %s %s" % (strategy.upper(), mode.upper())
    if req.confirm.strip() != phrase:
        raise HTTPException(status_code=422, detail="type exactly: %s" % phrase)
    if kill_switch.tripped:
        raise HTTPException(status_code=409, detail=(
            "kill switch is tripped (%s) — clear it first" % kill_switch.code))
    a = arm_store.arm(strategy, mode, by=req.by)
    algo_ledger.record("arm", datetime.now(IST).date().isoformat(), {
        "strategy": a.strategy, "mode": a.mode, "by": a.by,
        "expires_at": a.expires_at})
    return status()


@router.post("/disarm", dependencies=[Depends(require_control_token)])
def disarm(req: ReasonRequest) -> dict:
    had = arm_store.disarm(by=req.by)
    if had:
        algo_ledger.record("disarm", datetime.now(IST).date().isoformat(),
                           {"by": req.by, "reason": req.reason})
    return status()


@router.post("/kill")
def kill(req: ReasonRequest) -> dict:
    """No token: stopping the runner must always be possible."""
    new = kill_switch.trip("MANUAL", req.reason or "kill button", now=time.time())
    arm_store.disarm(by=req.by)
    if new:
        algo_ledger.record("kill", datetime.now(IST).date().isoformat(),
                           {"code": "MANUAL", "by": req.by, "reason": req.reason})
    return status()


@router.post("/clear", dependencies=[Depends(require_control_token)])
def clear(req: ReasonRequest) -> dict:
    if kill_switch.clear(by=req.by, now=time.time()):
        algo_ledger.record("clear", datetime.now(IST).date().isoformat(),
                           {"by": req.by, "reason": req.reason})
    return status()
