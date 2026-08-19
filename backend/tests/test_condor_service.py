"""CondorService.run_once off-session behavior.

The 60s condor loop runs whenever the feed is up — overnight and on weekends
too. Before the closed-market gate, every one of those minutes wrote a
{"no_trade": ["market closed"]} line into .condor_eval.jsonl, drowning the
actual dataset. These tests pin the gate: a closed-market run writes NO trace
line and evaluates only once (the final honest state served by
GET /condor/{symbol}), while card lifecycle keeps ticking and open-market
tracing is unchanged.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.condor.models import CondorCard
from app.condor.service import CondorService
from app.condor.store import CondorStore, EvalTrace
from app.config import Settings
from app.kite.instruments import OptionUniverse, chain_key
from app.market import calendar as mcal
from app.state import MarketState


def _service(tmp_path):
    cfg = Settings(_env_file=None, KITE_API_KEY="t", KITE_API_SECRET="t",
                   CONDOR_ENABLED="true")
    uni = OptionUniverse(symbol="NIFTY", expiry=date(2026, 8, 20), step=50)
    universes = {chain_key("NIFTY", "nearest"): uni}
    trace_path = tmp_path / "trace.jsonl"
    svc = CondorService(cfg, MarketState(), universes,
                        CondorStore(None, None), EvalTrace(trace_path))
    return svc, trace_path


def test_closed_market_writes_no_trace_and_evaluates_once(tmp_path, monkeypatch):
    svc, trace_path = _service(tmp_path)
    monkeypatch.setattr(mcal, "is_market_open", lambda dt=None: False)
    calls = {"n": 0}
    real_eval = svc.engine.evaluate

    def counting(*a, **k):
        calls["n"] += 1
        return real_eval(*a, **k)

    monkeypatch.setattr(svc.engine, "evaluate", counting)
    for _ in range(3):
        svc.run_once()
    assert not trace_path.exists(), "closed-market run_once must write no trace line"
    assert calls["n"] == 1, "one final closed-state evaluation, then idle"
    resp = svc.last_responses["NIFTY"]
    assert "market closed" in (resp["no_trade_reasons"] or [])
    assert resp["data_ok"] is False


def test_open_market_tracing_unchanged(tmp_path, monkeypatch):
    svc, trace_path = _service(tmp_path)
    monkeypatch.setattr(mcal, "is_market_open", lambda dt=None: True)
    svc.run_once()
    lines = trace_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["symbol"] == "NIFTY"


def test_reopen_after_close_publishes_and_traces_again(tmp_path, monkeypatch):
    svc, trace_path = _service(tmp_path)
    market = {"open": False}
    monkeypatch.setattr(mcal, "is_market_open", lambda dt=None: market["open"])
    svc.run_once()
    assert "market closed" in (svc.last_responses["NIFTY"]["no_trade_reasons"] or [])
    assert not trace_path.exists()
    # Next open must clear the published flag and resume normal tracing …
    market["open"] = True
    svc.run_once()
    assert len(trace_path.read_text().splitlines()) == 1
    assert "market closed" not in (svc.last_responses["NIFTY"]["no_trade_reasons"] or [])
    # … and the following close must publish a fresh final closed state.
    market["open"] = False
    # EvalTrace dedupes per (symbol, minute); pin that the closed run writes
    # nothing even in a fresh minute, not because dedupe swallowed it.
    svc.trace._last_minute.clear()
    svc.run_once()
    assert "market closed" in (svc.last_responses["NIFTY"]["no_trade_reasons"] or [])
    assert len(trace_path.read_text().splitlines()) == 1


def test_card_still_expires_while_closed(tmp_path, monkeypatch):
    svc, _ = _service(tmp_path)
    monkeypatch.setattr(mcal, "is_market_open", lambda dt=None: False)
    svc.store.cards["NIFTY"] = CondorCard(
        id="c1", symbol="NIFTY", valid_until=int(time.time()) - 10)
    svc.run_once()
    card = svc.store.cards["NIFTY"]
    assert card.state == "expired", "reconcile must keep aging cards off-session"
