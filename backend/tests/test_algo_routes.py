"""/algo routes (16-Sep): the control token, the typed arm, the tokenless kill.

The route layer is where the module meets an API that otherwise has no auth,
so these pin the auth posture rather than the payloads: every mutating route
refuses without the token, refuses when the token is unset, KILL works with
no token at all, and an arm needs the exact phrase — a click is not enough.

Run:  python backend/tests/test_algo_routes.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.algo.arm import ArmStore
from app.algo.killswitch import KillSwitch
from app.algo.ledger import AlgoLedger
from app.api import routes_algo as R

TOKEN = "t3st-t0ken"
HDR = {"X-Algo-Token": TOKEN}


class _Cfg:
    def __init__(self, token):
        self.algo_control_token = token
        self.algo_poll_s = 15.0


def _client(td: Path, token=TOKEN):
    """A bare app with just the algo router, singletons swapped for temp ones
    and the live world stubbed — no Kite, no feed, no real state files."""
    R.get_settings = lambda: _Cfg(token)
    R.arm_store = ArmStore()
    R.kill_switch = KillSwitch(td / ".k.json")
    R.algo_ledger = AlgoLedger(td / ".l.jsonl")
    R.live_world = lambda now: dict(trading_day=True, token_state="valid",
                                    feed_healthy=True, last_tick_age_s=1.0,
                                    clock_skew_s=0.4, day_pnl_rs=0.0,
                                    reconciled_at=None)
    R.closing_adapter.logged_card = lambda d: None
    app = FastAPI()
    app.include_router(R.router)
    return TestClient(app)


def test_status_is_open_and_describes_the_whole_posture():
    with tempfile.TemporaryDirectory() as td:
        c = _client(Path(td))
        r = c.get("/algo/status")
        assert r.status_code == 200
        s = r.json()
        assert s["arm"] is None and s["kill"]["tripped"] is False
        assert s["contract"]["reachable_modes"] == ["dry"]
        assert s["contract"]["unset_live_caps"]
        keys = {p["key"] for p in s["preflight"]}
        assert {"control_token", "kill_switch", "token", "feed", "clock", "live_caps"} <= keys
        assert s["adapters"]["closing"]["would_fire"] is False
        assert len(s["checks"]) == 28
    print("  ROUTE  -> /algo/status: arm, kill, preflight, contract, checks")


def test_mutating_routes_refuse_without_the_token_and_when_it_is_unset():
    body = {"strategy": "closing", "mode": "dry", "confirm": "ARM CLOSING DRY"}
    with tempfile.TemporaryDirectory() as td:
        c = _client(Path(td))
        assert c.post("/algo/arm", json=body).status_code == 403
        assert c.post("/algo/arm", json=body, headers={"X-Algo-Token": "wrong"}).status_code == 403
        assert c.post("/algo/disarm", json={}).status_code == 403
        assert c.post("/algo/clear", json={}).status_code == 403
        assert c.post("/algo/arm", json=body, headers=HDR).status_code == 200
    with tempfile.TemporaryDirectory() as td:
        c = _client(Path(td), token="")                      # unset in .env
        r = c.post("/algo/arm", json=body, headers=HDR)
        assert r.status_code == 403 and "not set" in r.json()["detail"]
    print("  ROUTE  -> arm/disarm/clear: 403 without token; unset token refuses")


def test_arm_needs_the_exact_phrase_and_only_dry_is_reachable():
    with tempfile.TemporaryDirectory() as td:
        c = _client(Path(td))
        base = {"strategy": "closing", "mode": "dry"}
        assert c.post("/algo/arm", json=dict(base, confirm="arm closing dry"),
                      headers=HDR).status_code == 422
        assert c.post("/algo/arm", json=dict(base, confirm="yes"), headers=HDR).status_code == 422
        for mode in ("paper", "live"):
            r = c.post("/algo/arm", json={"strategy": "closing", "mode": mode,
                                          "confirm": "ARM CLOSING %s" % mode.upper()},
                       headers=HDR)
            assert r.status_code == 409, (mode, r.json())
        assert c.post("/algo/arm", json={"strategy": "gold", "mode": "dry",
                                         "confirm": "ARM GOLD DRY"}, headers=HDR).status_code == 422
        r = c.post("/algo/arm", json=dict(base, confirm="ARM CLOSING DRY"), headers=HDR)
        assert r.status_code == 200 and r.json()["arm"]["mode"] == "dry"
        assert [x["type"] for x in R.algo_ledger.rows()] == ["arm"]
    print("  ROUTE  -> arm: exact phrase, dry only; paper/live 409; unknown 422")


def test_kill_needs_no_token_and_blocks_arming_until_cleared():
    with tempfile.TemporaryDirectory() as td:
        c = _client(Path(td))
        body = {"strategy": "closing", "mode": "dry", "confirm": "ARM CLOSING DRY"}
        assert c.post("/algo/arm", json=body, headers=HDR).status_code == 200
        r = c.post("/algo/kill", json={"reason": "smells wrong"})       # no header
        assert r.status_code == 200
        s = r.json()
        assert s["kill"]["tripped"] and s["kill"]["code"] == "MANUAL" and s["arm"] is None
        r = c.post("/algo/arm", json=body, headers=HDR)
        assert r.status_code == 409 and "clear it first" in r.json()["detail"]
        assert c.post("/algo/clear", json={"by": "shibin"}).status_code == 403
        r = c.post("/algo/clear", json={"by": "shibin"}, headers=HDR)
        assert r.status_code == 200 and r.json()["kill"]["tripped"] is False
        assert c.post("/algo/arm", json=body, headers=HDR).status_code == 200
        types = [x["type"] for x in R.algo_ledger.rows()]
        assert types == ["arm", "kill", "clear", "arm"], types
    print("  ROUTE  -> kill: tokenless, disarms, blocks arm; clear is gated")


def test_ledger_route_is_newest_first_and_day_scoped():
    with tempfile.TemporaryDirectory() as td:
        c = _client(Path(td))
        R.algo_ledger.record("skip", "2026-09-15", {"key": "a"})
        R.algo_ledger.record("skip", "2026-09-16", {"key": "b"})
        r = c.get("/algo/ledger").json()
        assert [x["key"] for x in r["rows"]] == ["b", "a"]
        assert c.get("/algo/ledger?day=2026-09-15").json()["count"] == 1
    print("  ROUTE  -> /algo/ledger: newest first, ?day= filters")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
