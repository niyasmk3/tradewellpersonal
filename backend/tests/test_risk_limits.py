"""Runtime trading-settings store tests.

Once these were the live loss guards; the circuit breakers were removed 25-Jul
(see risk_limits.py) and two non-breaker settings remain — the day's trading
fund and the paper simulator's position cap. The store that holds them must
still: apply an override live, fall back to .env when unset, reject nonsense,
persist across a restart, and never smuggle in an unknown key.

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
    base = {"SIGNAL_MAX_OPEN_POSITIONS": 2, "TRADING_FUND": 0}
    base.update(over)
    return Settings(_env_file=None, **base)


def _store():
    TMP.unlink(missing_ok=True)
    return RiskLimitStore(path=TMP)


def test_only_fund_and_paper_cap_remain():
    """The breakers are gone: SPECS is exactly the two surviving settings."""
    keys = {s.key for s in SPECS}
    assert keys == {"trading_fund", "max_open_positions"}, keys
    print("  RISK   -> SPECS = trading_fund + max_open_positions (breakers removed)")


def test_falls_back_to_env_when_unset():
    cfg = _cfg(TRADING_FUND=1_000_000, SIGNAL_MAX_OPEN_POSITIONS=3)
    eff = _store().effective(cfg)
    assert eff["trading_fund"] == 1_000_000, eff
    assert eff["max_open_positions"] == 3, eff
    print("  RISK   -> unset settings read straight from .env")


def test_override_wins_over_env():
    cfg = _cfg(SIGNAL_MAX_OPEN_POSITIONS=2)
    s = _store()
    s.set_many({"max_open_positions": 4}, cfg)
    assert s.effective(cfg)["max_open_positions"] == 4
    print("  RISK   -> an override beats the .env default")


def test_override_persists_across_reload():
    cfg = _cfg()
    _store().set_many({"trading_fund": 900_000, "max_open_positions": 3}, cfg)
    reloaded = RiskLimitStore(path=TMP)                 # simulates a restart
    eff = reloaded.effective(cfg)
    assert eff["trading_fund"] == 900_000 and eff["max_open_positions"] == 3
    print("  RISK   -> overrides survive a backend restart")


def test_trading_fund_round_trips():
    """The fund is edited from the same UI panel; a SPECS entry that view()
    renders but set_many/PUT drops is a field whose saves silently vanish —
    that exact bug shipped once (RiskLimitUpdate missing the key)."""
    cfg = _cfg(TRADING_FUND=0)
    s = _store()
    s.set_many({"trading_fund": 1_000_000.0}, cfg)
    assert s.effective(cfg)["trading_fund"] == 1_000_000.0
    reloaded = RiskLimitStore(path=TMP)                 # survives a restart
    assert reloaded.effective(cfg)["trading_fund"] == 1_000_000.0
    field = next(f for f in reloaded.view(cfg)["fields"] if f["key"] == "trading_fund")
    assert field["unit"] == "rupees" and field["source"] == "override"
    # Over the spec ceiling must be rejected.
    try:
        s.set_many({"trading_fund": 200_000_000.0}, cfg)
        raise AssertionError("accepted a fund above the spec maximum")
    except ValueError:
        pass
    # The PUT model must accept both surviving keys end-to-end.
    from app.api.routes_settings import RiskLimitUpdate
    body = RiskLimitUpdate(trading_fund=500_000.0, max_open_positions=3)
    dumped = body.model_dump()
    assert dumped["trading_fund"] == 500_000.0 and dumped["max_open_positions"] == 3
    print("  RISK   -> trading_fund: set, persisted, viewed, bounded, PUT-accepted")


def test_put_model_mirrors_specs():
    """RiskLimitUpdate must mirror SPECS key-for-key — a missing key is a field
    whose UI saves silently never persist."""
    from app.api.routes_settings import RiskLimitUpdate
    model_keys = set(RiskLimitUpdate.model_fields)
    assert model_keys == {s.key for s in SPECS}, model_keys
    print("  RISK   -> PUT model mirrors SPECS exactly")


def test_counts_are_stored_as_whole_numbers():
    cfg = _cfg()
    s = _store()
    s.set_many({"max_open_positions": 3.9}, cfg)
    # A fractional position count is meaningless; it must be truncated, not kept.
    assert s.effective(cfg)["max_open_positions"] == 3.0
    print("  RISK   -> position counts stored whole")


def test_rejects_out_of_range_and_unknown_keys():
    cfg = _cfg()
    s = _store()
    for bad in ({"trading_fund": -1}, {"max_open_positions": 999}):
        try:
            s.set_many(bad, cfg)
            assert False, f"accepted out-of-range {bad}"
        except ValueError:
            pass
    for gone in ({"score_valid": 90}, {"daily_loss_limit": 8000},
                 {"max_consecutive_losses": 2}):
        try:
            s.set_many(gone, cfg)                       # removed / never editable
            assert False, f"accepted an unknown key {gone}"
        except ValueError:
            pass
    print("  RISK   -> out-of-range values and removed/unknown keys refused")


def test_a_corrupt_overlay_falls_back_not_crashes():
    TMP.write_text("{ this is not json ")
    cfg = _cfg(TRADING_FUND=12000)
    eff = RiskLimitStore(path=TMP).effective(cfg)
    assert eff["trading_fund"] == 12000, "corrupt overlay must fall back to .env"
    print("  RISK   -> corrupt overlay ignored, .env used")


def test_stale_overlay_keys_are_dropped_on_load():
    # A removed breaker key left in an old overlay must be dropped, not served.
    TMP.write_text('{"overrides": {"max_open_positions": 3, "daily_loss_limit": 7000, '
                   '"gone_field": 5}, "updated_at": 1}')
    cfg = _cfg()
    eff = RiskLimitStore(path=TMP).effective(cfg)
    assert eff["max_open_positions"] == 3
    assert "daily_loss_limit" not in eff and "gone_field" not in eff
    print("  RISK   -> removed/unknown keys in an old overlay are dropped")


def test_view_shape_and_no_order_note():
    cfg = _cfg(SIGNAL_MAX_OPEN_POSITIONS=0)
    s = _store()
    s.set_many({"trading_fund": 10000}, cfg)
    view = s.view(cfg)
    assert len(view["fields"]) == len(SPECS) == 2
    tf = next(f for f in view["fields"] if f["key"] == "trading_fund")
    assert tf["value"] == 10000 and tf["source"] == "override" and tf["description"]
    # The note no longer claims the settings halt signals (they don't any more),
    # but must still make the no-order point.
    assert "HALT SIGNALS" not in view["note"]
    assert "no order" in view["note"].lower() or "nothing" in view["note"].lower()
    # A zero paper cap reports as disabled (zero_disables).
    mp = next(f for f in view["fields"] if f["key"] == "max_open_positions")
    assert mp["disabled"] is True
    print("  RISK   -> view has values, sources, descriptions, and the no-order note")


def test_paper_cap_reads_the_store_live():
    """The paper simulator reads max_open_positions from the store each cycle,
    so an edit takes effect with no restart — the property that used to matter
    for the breakers now matters for the sim cap."""
    cfg = _cfg(SIGNAL_MAX_OPEN_POSITIONS=2)
    s = _store()
    assert int(s.effective(cfg)["max_open_positions"]) == 2
    s.set_many({"max_open_positions": 1}, cfg)
    assert int(s.effective(cfg)["max_open_positions"]) == 1, "edit not reflected live"
    print("  RISK   -> paper cap reflects an edit on the very next cycle")


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
