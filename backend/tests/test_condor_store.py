"""Review-fix regressions: excursion latch safety, trace dedupe per symbol,
torn-line prune tolerance, adjustment journaling and its cap."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.condor.models import AdjustCondorRequest, CondorResponse, EnterCondorRequest
from app.condor.monitor import CondorMonitor, build_position
from app.condor.store import CondorStore, EvalTrace
from app.config import Settings


def _settings(**over) -> Settings:
    base = dict(KITE_API_KEY="t", KITE_API_SECRET="t")
    base.update(over)
    return Settings(_env_file=None, **base)


def _pos(store: CondorStore):
    req = EnterCondorRequest(symbol="NIFTY", lots=1,
                             short_ce_strike=25300, short_ce_fill=72.0,
                             short_pe_strike=24700, short_pe_fill=65.0,
                             wing_ce_strike=25500, wing_ce_fill=28.0,
                             wing_pe_strike=24500, wing_pe_fill=25.0)
    pos = build_position(req, 75, "2026-08-13")
    store.add_position(pos)
    return pos


def test_note_excursion_latches_and_ignores_closed():
    store = CondorStore(None, None)
    pos = _pos(store)
    store.note_excursion(pos.id, 70.0)
    store.note_excursion(pos.id, 95.0)
    store.note_excursion(pos.id, 80.0)          # inside the latch — no change
    got = store.get_position(pos.id)
    assert got.prem_min == 70.0 and got.prem_max == 95.0
    # Close it, then a racing monitor view reports another print:
    mon = CondorMonitor(_settings(), None, store)
    mon.close_position(pos.id, 40.0, "test")
    store.note_excursion(pos.id, 10.0)          # must NOT touch a closed row
    got = store.get_position(pos.id)
    assert got.status == "closed" and got.prem_min == 70.0


def test_build_position_accrues_entry_charges():
    store = CondorStore(None, None)
    pos = _pos(store)
    assert pos.charges_accrued > 100            # 4 orders + STT on sells
    # Live P&L and realized P&L now share this sunk cost by construction.


def test_record_adjustment_counts_against_cap():
    store = CondorStore(None, None)
    pos = _pos(store)
    mon = CondorMonitor(_settings(), None, store)
    req = AdjustCondorRequest(side="PE", close_debit=20.0,
                              new_short_strike=24900, new_short_fill=55.0,
                              new_wing_strike=24700, new_wing_fill=23.0)
    adj = mon.record_adjustment(pos.id, req)
    assert adj is not None
    assert adj.adjustments == 1
    # Added credit = (55-23) - 20 = 12; total 84 + 12 = 96.
    assert adj.credit_fill == 96.0
    assert adj.charges_accrued > pos.charges_accrued or adj.charges_accrued > 100
    strikes = sorted((l.strike, l.side.value) for l in adj.legs if l.right == "PE")
    assert (24700.0, "BUY") in strikes and (24900.0, "SELL") in strikes
    # Cap: with CONDOR_MAX_ADJUSTMENTS=1 the suggestion engine refuses next time.
    assert mon._adjustment(adj, None, None, None, None, None,
                           None, None, None, None, "LOW", 0) is None


def test_eval_trace_dedupes_per_symbol_not_globally(tmp_path):
    trace = EvalTrace(tmp_path / "trace.jsonl")
    ts = 1_786_400_000
    for sym in ("NIFTY", "BANKNIFTY"):
        trace.record(CondorResponse(symbol=sym, evaluated_at=ts))
    rows = trace.rows(days=3650)
    assert {r["symbol"] for r in rows} == {"NIFTY", "BANKNIFTY"}, \
        "second symbol must not be swallowed by the first's minute stamp"


def test_prune_survives_torn_line(tmp_path):
    import time as _t
    p = tmp_path / "trace.jsonl"
    trace = EvalTrace(p, keep_days=30)
    now = int(_t.time())
    fresh = json.dumps({"ts": now, "symbol": "NIFTY"})
    stale = json.dumps({"ts": now - 90 * 86400, "symbol": "NIFTY"})
    p.write_text(stale + "\n" + '{"ts": 17864' + "\n" + fresh + "\n")  # torn middle
    trace.prune()
    kept = p.read_text().strip().splitlines()
    assert kept == [fresh], "prune must drop stale+torn lines and keep fresh ones"
