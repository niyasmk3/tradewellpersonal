"""Runtime risk-limit store tests.

These guards are the last line between a bad day and a worse one, so the store
that holds them must: apply an override live, fall back to .env when unset,
reject nonsense, persist across a restart, and never smuggle in an unknown key.

Run:  python backend/tests/test_risk_limits.py
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import Settings
from app.signals.risk_limits import RiskLimitStore, SPECS

TMP = pathlib.Path("/tmp/tw-risk-limits-test.json")


def _cfg(**over):
    base = {"SIGNAL_DAILY_LOSS_LIMIT": 0.0, "SIGNAL_MAX_OPEN_DRAWDOWN": 0.0,
            "SIGNAL_MAX_CONSECUTIVE_LOSSES": 2, "SIGNAL_MAX_OPEN_POSITIONS": 2}
    base.update(over)
    return Settings(_env_file=None, **base)


def _store():
    TMP.unlink(missing_ok=True)
    return RiskLimitStore(path=TMP)


def test_falls_back_to_env_when_unset():
    cfg = _cfg(SIGNAL_DAILY_LOSS_LIMIT=15000, SIGNAL_MAX_OPEN_POSITIONS=3)
    eff = _store().effective(cfg)
    assert eff["daily_loss_limit"] == 15000, eff
    assert eff["max_open_positions"] == 3, eff
    print("  RISK   -> unset limits read straight from .env")


def test_override_wins_over_env():
    cfg = _cfg(SIGNAL_DAILY_LOSS_LIMIT=15000)
    s = _store()
    s.set_many({"daily_loss_limit": 8000}, cfg)
    assert s.effective(cfg)["daily_loss_limit"] == 8000
    print("  RISK   -> an override beats the .env default")


def test_override_persists_across_reload():
    cfg = _cfg()
    _store().set_many({"daily_loss_limit": 9000, "max_open_drawdown": 6000}, cfg)
    reloaded = RiskLimitStore(path=TMP)                 # simulates a restart
    eff = reloaded.effective(cfg)
    assert eff["daily_loss_limit"] == 9000 and eff["max_open_drawdown"] == 6000
    print("  RISK   -> overrides survive a backend restart")


def test_counts_are_stored_as_whole_numbers():
    cfg = _cfg()
    s = _store()
    s.set_many({"max_open_positions": 3.0}, cfg)
    assert s.effective(cfg)["max_open_positions"] == 3
    # A fractional count would be meaningless; it must be truncated, not kept.
    s.set_many({"max_consecutive_losses": 2.9}, cfg)
    assert s.effective(cfg)["max_consecutive_losses"] == 2.0
    print("  RISK   -> position/streak counts stored whole")


def test_rejects_out_of_range_and_unknown_keys():
    cfg = _cfg()
    s = _store()
    for bad in ({"daily_loss_limit": -1}, {"max_open_positions": 999},
                {"max_consecutive_losses": 0}):
        try:
            s.set_many(bad, cfg)
            assert False, f"accepted out-of-range {bad}"
        except ValueError:
            pass
    try:
        s.set_many({"score_valid": 90}, cfg)          # not an editable limit
        assert False, "accepted an unknown key"
    except ValueError:
        pass
    print("  RISK   -> out-of-range values and unknown keys refused")


def test_a_corrupt_overlay_falls_back_not_crashes():
    TMP.write_text("{ this is not json ")
    cfg = _cfg(SIGNAL_DAILY_LOSS_LIMIT=12000)
    eff = RiskLimitStore(path=TMP).effective(cfg)
    assert eff["daily_loss_limit"] == 12000, "corrupt overlay must fall back to .env"
    print("  RISK   -> corrupt overlay ignored, .env used")


def test_stale_overlay_keys_are_dropped_on_load():
    TMP.write_text('{"overrides": {"daily_loss_limit": 7000, "gone_field": 5}, "updated_at": 1}')
    cfg = _cfg()
    eff = RiskLimitStore(path=TMP).effective(cfg)
    assert eff["daily_loss_limit"] == 7000
    assert "gone_field" not in eff
    print("  RISK   -> a removed field in an old overlay is dropped")


def test_view_carries_description_and_the_no_order_note():
    cfg = _cfg()
    s = _store()
    s.set_many({"daily_loss_limit": 10000}, cfg)
    view = s.view(cfg)
    assert len(view["fields"]) == len(SPECS)
    dl = next(f for f in view["fields"] if f["key"] == "daily_loss_limit")
    assert dl["value"] == 10000 and dl["source"] == "override"
    assert dl["env_default"] == 0.0 and dl["description"]
    # The recurring honesty point must be present and unmissable.
    assert "HALT SIGNALS" in view["note"] and "no order" in view["note"].lower()
    # A zero rupee limit reports as disabled.
    dd = next(f for f in view["fields"] if f["key"] == "max_open_drawdown")
    assert dd["disabled"] is True
    print("  RISK   -> view has values, sources, descriptions, and the no-order note")


def test_throttle_reads_limits_live():
    """The whole point: a limit set now must change the NEXT evaluation without
    a restart. Exercises SignalService._throttle against the real store."""
    from app.signals.service import SignalService
    import app.signals.service as svc_mod

    cfg = _cfg(SIGNAL_DAILY_LOSS_LIMIT=0)
    svc = SignalService.__new__(SignalService)         # skip __init__ (needs state)
    svc.cfg = cfg
    store = _store()
    svc_mod.risk_limit_store = store                    # point the service at ours
    assert svc._throttle().daily_loss_limit == 0.0
    store.set_many({"daily_loss_limit": 10000}, cfg)
    assert svc._throttle().daily_loss_limit == 10000.0, "throttle did not pick up the live edit"
    print("  RISK   -> throttle reflects an edit on the very next cycle")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    TMP.unlink(missing_ok=True)
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
