"""Strategy Lab learning tests — the evidence gates, pinned.

  * proposals need >= 30 trades of evidence and stay inside param bounds;
  * a challenger with too few TEST trades is rejected, whatever its P&L;
  * promotion needs forward paper evidence and stays inside paper;
  * every decision leaves an experiment row with its reason.

Run:  python backend/tests/test_lab_learning.py   (also pytest-compatible)
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))

from app.lab import learning
from app.lab.catalog import get as cat_get
from app.lab.models import validate_params
from app.lab.store import LabStore


def _summary(trades=60, expectancy=-50.0, stop_frac=0.6):
    n_stop = int(trades * stop_frac)
    return {
        "trades": trades, "expectancy": expectancy, "net_pnl":
            expectancy * trades,
        "by_exit_reason": {
            "stop": {"n": n_stop, "net": -8000, "expectancy": -120,
                     "wins": 0},
            "target": {"n": trades - n_stop, "net": 3000, "expectancy": 60,
                       "wins": trades - n_stop},
        },
        "by_regime": {},
        "max_drawdown": -5000,
    }


def test_no_proposal_without_evidence():
    params = validate_params(cat_get("F02").params, {})
    assert learning.propose_from_evidence("F02", _summary(trades=10),
                                          params) is None, \
        "10 trades must never justify a hypothesis"


def test_stop_heavy_proposal_widens_stop_within_bounds():
    params = validate_params(cat_get("F02").params, {})
    prop = learning.propose_from_evidence("F02", _summary(), params)
    assert prop is not None
    new_params, hypothesis, evidence = prop
    assert new_params["stop_atr"] > params["stop_atr"]
    assert "widen stop_atr" in hypothesis
    # bounds hold even from the top of the range
    hi_params = dict(params)
    hi_params["stop_atr"] = 5.0     # spec max
    prop2 = learning.propose_from_evidence("F02", _summary(), hi_params)
    assert prop2 is None or prop2[0]["stop_atr"] <= 5.0


def test_validation_rejects_thin_test_segment():
    with tempfile.TemporaryDirectory() as td:
        st = LabStore(Path(td) / "lab.db")
        params = validate_params(cat_get("F02").params, {})
        st.ensure_version1("F02", params)
        v2 = st.add_version("F02", params, "learning", 1, "test")
        exp_id = st.create_experiment("F02", 1, v2, "hyp", {})
        exp = st.experiments("F02")[0]

        orig_run, orig_ready = learning.run_span, None
        from app.lab import data as data_mod
        orig_ready = data_mod.strategy_readiness
        data_mod.strategy_readiness = lambda needs, ready=None: {
            "status": "ready", "from": "2026-08-01", "to": "2026-09-20",
            "notes": []}
        learning.run_span = lambda sid, ver, p, lo, hi, kind, capital=0, \
            store=None: {"expectancy": 500.0, "trades": 3,
                         "max_drawdown": -100}
        try:
            learning.validate_experiment(exp, st)
        finally:
            learning.run_span = orig_run
            data_mod.strategy_readiness = orig_ready
        after = st.experiments("F02")[0]
        assert after["status"] == "rejected"
        assert "insufficient" in after["decision_reason"], \
            after["decision_reason"]


def test_paper_promotion_needs_days_and_beats_champion():
    with tempfile.TemporaryDirectory() as td:
        st = LabStore(Path(td) / "lab.db")
        params = validate_params(cat_get("F02").params, {})
        st.ensure_version1("F02", params)
        v2 = st.add_version("F02", params, "learning", 1, "test")
        exp_id = st.create_experiment("F02", 1, v2, "hyp", {})
        st.update_experiment(exp_id, status="paper")
        st.set_state("F02", experimental_version=v2)
        exp = st.experiments("F02")[0]

        # 5 days only -> no decision yet
        for i in range(5):
            day = "2026-09-%02d" % (i + 1)
            st.save_paper_day(day, "F02", v2, 2, 100.0, {})
            st.save_paper_day(day, "F02", 1, 2, 10.0, {})
        learning.evaluate_paper_arm(exp, st)
        assert st.experiments("F02")[0]["status"] == "paper", \
            "5 days must not decide anything"

        for i in range(5, 12):
            day = "2026-09-%02d" % (i + 1)
            st.save_paper_day(day, "F02", v2, 2, 100.0, {})
            st.save_paper_day(day, "F02", 1, 2, 10.0, {})
        learning.evaluate_paper_arm(st.experiments("F02")[0], st)
        after = st.experiments("F02")[0]
        assert after["status"] == "accepted", after
        state = st.state("F02")
        assert state["champion_version"] == v2
        assert state["experimental_version"] is None
        # reversibility: the old champion's params are still there
        assert st.get_params("F02", 1) is not None


def test_losing_paper_arm_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        st = LabStore(Path(td) / "lab.db")
        params = validate_params(cat_get("F02").params, {})
        st.ensure_version1("F02", params)
        v2 = st.add_version("F02", params, "learning", 1, "test")
        exp_id = st.create_experiment("F02", 1, v2, "hyp", {})
        st.update_experiment(exp_id, status="paper")
        st.set_state("F02", experimental_version=v2)
        for i in range(12):
            day = "2026-09-%02d" % (i + 1)
            st.save_paper_day(day, "F02", v2, 2, -50.0, {})
            st.save_paper_day(day, "F02", 1, 2, 10.0, {})
        learning.evaluate_paper_arm(st.experiments("F02")[0], st)
        after = st.experiments("F02")[0]
        assert after["status"] == "rejected"
        state = st.state("F02")
        assert state["champion_version"] == 1
        assert state["experimental_version"] is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print("%d tests passed" % len(fns))
