"""Post-close ops (05-Aug): state backup and verdict-threshold watch.

Run:  python backend/tests/test_ops.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from app.ops import (
    IST, backup_paths, load_state, make_backup, run_daily_ops_once, save_state,
    verdict_events,
)


def _seed_state_dir(d: Path) -> None:
    (d / ".env").write_text("SECRET=1")
    (d / ".paper_trades.json").write_text("[]")
    (d / ".signals_archive.jsonl").write_text("{}\n")
    (d / ".patterns_candles.db").write_text("db")
    (d / ".instruments_cache.json").write_text("{}")     # excluded: regenerable
    (d / "app.py").write_text("code")                    # excluded: not state


def test_backup_selects_state_files_only():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _seed_state_dir(d)
        names = {p.name for p in backup_paths(d)}
        assert names == {".env", ".paper_trades.json", ".signals_archive.jsonl",
                         ".patterns_candles.db"}, names
    print("  PATHS  -> state files in, caches and code out")


def test_backup_tarball_and_prune():
    with tempfile.TemporaryDirectory() as td:
        d, dest = Path(td) / "src", Path(td) / "icloud"
        d.mkdir()
        _seed_state_dir(d)
        for day in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"):
            out = make_backup(d, dest, day, keep=3)
            assert out.exists()
        kept = sorted(p.name for p in dest.glob("tradewell-state-*.tgz"))
        assert kept == ["tradewell-state-2026-08-02.tgz",
                        "tradewell-state-2026-08-03.tgz",
                        "tradewell-state-2026-08-04.tgz"], kept
        with tarfile.open(dest / kept[-1]) as tar:
            members = set(tar.getnames())
        assert ".env" in members and ".paper_trades.json" in members, members
        assert ".instruments_cache.json" not in members
    print("  TAR    -> daily tarball written, oldest pruned to keep")


def test_verdict_thresholds_and_once_only():
    summary = {
        "exit_ab": {"n_diverged": 30, "verdict": "Ratchet ahead."},
        "stop_ab": {"n_diverged": 29},
        "stop_calib": None,
        "by_mode": {"scalp": {"trades": 50, "net_pnl": 900.0,
                              "expectancy": 18.0, "win_rate": 55.0}},
        "hollow": {"trades": 30, "net_pnl": -1200.0,
                   "expectancy": -40.0, "win_rate": 30.0},
        "late_shadow": {"trades": 12},
        "refire_shadow": None,
        "derisk_aftermath": {"locked_out": 10, "note": "n/a"},
    }
    events = verdict_events(summary, announced=set())
    keys = {e["key"] for e in events}
    # At the bar: exit_ab (30 diverged), scalp (50), floor (30), derisk (10).
    # Below it: stop_ab (29), late (12). Absent blocks never fire.
    assert keys == {"exit_ab_30", "scalp_50", "floor_30", "derisk_10"}, keys
    # Once-ever: an announced key never fires again.
    again = verdict_events(summary, announced=keys)
    assert again == [], again
    # Bodies carry the ledger's own verdict text and the decision knob.
    ab = next(e for e in events if e["key"] == "exit_ab_30")
    assert "Ratchet ahead." in ab["body"] and "QUICK_BANK" in ab["body"], ab
    print("  BAR    -> thresholds fire at the bar, once ever, with the verdict")


def test_state_roundtrip_and_time_gate():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".daily_ops.json"
        s = load_state(p)
        assert s["announced"] == [] and s["backup_date"] is None
        s["backup_date"] = "2026-08-05"
        s["announced"] = ["scalp_50"]
        save_state(s, p)
        assert load_state(p)["announced"] == ["scalp_50"]
    # Before 15:40 IST the pass is a pure no-op — no settings, no disk.
    out = run_daily_ops_once(now=datetime(2026, 8, 5, 11, 0, tzinfo=IST))
    assert out == {"backup": None, "verdicts": []}, out
    print("  GATE   -> state survives a round-trip; pre-close pass is a no-op")


def test_daily_pass_backs_up_once():
    import app.config as config_mod
    import app.services as services_mod

    class _Cfg:
        state_backup_dir = ""       # set below
        state_backup_keep = 5
        paper_slippage_pct = 0.0
        quick_bank_single_lot = False

    _MISSING = object()
    orig_get = config_mod.get_settings
    orig_store = getattr(services_mod.feed, "paper_store", _MISSING)
    cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as td:
            d, dest = Path(td) / "backend", Path(td) / "icloud"
            d.mkdir()
            _seed_state_dir(d)
            os.chdir(d)
            _Cfg.state_backup_dir = str(dest)
            config_mod.get_settings = lambda: _Cfg()
            # No paper store: the verdict half waits, the backup half runs.
            services_mod.feed.paper_store = None
            now = datetime(2026, 8, 5, 15, 45, tzinfo=IST)
            out1 = run_daily_ops_once(now=now)
            assert out1["backup"] and Path(out1["backup"]).exists(), out1
            # Second pass same day: self-gated, nothing re-written.
            out2 = run_daily_ops_once(now=now)
            assert out2["backup"] is None, out2
    finally:
        os.chdir(cwd)
        config_mod.get_settings = orig_get
        if orig_store is _MISSING:
            delattr(services_mod.feed, "paper_store")
        else:
            services_mod.feed.paper_store = orig_store
    print("  DAILY  -> one backup per IST day, second pass is a no-op")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
