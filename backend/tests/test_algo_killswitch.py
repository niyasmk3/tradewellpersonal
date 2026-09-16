"""Kill switch + arm state (16-Sep).

The two halves of "may this process trade right now", and they persist in
opposite directions on purpose:

  * the KILL SWITCH survives a restart, because "restart it and the error goes
    away" is how a broken algo gets a second go at the same mistake;
  * the ARM does NOT survive a restart, because a fresh process must never
    come up trading without a human in the room.

Run:  python backend/tests/test_algo_killswitch.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tempfile
from pathlib import Path

from app.algo import contract
from app.algo.arm import ArmStore
from app.algo.killswitch import KillSwitch

T0 = 1_000_000.0


def test_trip_latches_and_survives_restart():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".algo_killswitch.json"
        k = KillSwitch(p)
        assert k.tripped is False
        assert k.trip("RECONCILE_DRIFT", "position we never opened", now=T0) is True
        assert k.tripped and k.code == "RECONCILE_DRIFT"
        # A fresh object over the same file is a process restart.
        assert KillSwitch(p).tripped is True
    print("  KILL   -> trip latches to disk and survives a restart")


def test_second_trip_does_not_bury_the_first_cause():
    with tempfile.TemporaryDirectory() as td:
        k = KillSwitch(Path(td) / ".k.json")
        assert k.trip("FIRST", "the real cause", now=T0) is True
        assert k.trip("SECOND", "a consequence", now=T0 + 1) is False
        assert k.code == "FIRST"
    print("  KILL   -> only the first trip pages; the original cause is kept")


def test_zero_timestamp_is_honoured_not_replaced_by_wall_clock():
    """`float(now or time.time())` would silently swap a 0.0 epoch for the
    wall clock — the falsy-zero bug this module was written around."""
    with tempfile.TemporaryDirectory() as td:
        k = KillSwitch(Path(td) / ".k.json")
        k.trip("X", "", now=0.0)
        assert k.state()["tripped_at"] == 0.0, k.state()
    print("  KILL   -> now=0.0 is a timestamp, not a missing argument")


def test_only_an_explicit_clear_unlatches_and_it_is_recorded():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".k.json"
        k = KillSwitch(p)
        k.trip("DAILY_LOSS", "cap hit", now=T0)
        assert k.clear("shibin", now=T0 + 60) is True
        assert k.tripped is False
        assert KillSwitch(p).tripped is False
        assert k.state()["previous"]["code"] == "DAILY_LOSS"
        assert k.clear("shibin", now=T0 + 61) is False      # nothing to clear
    print("  KILL   -> clear needs a human, is persisted, and keeps the cause")


def test_unreadable_latch_file_reads_as_tripped():
    """The one interpretation we cannot afford is 'corrupt, so carry on'."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".k.json"
        p.write_text("{not json at all")
        k = KillSwitch(p)
        assert k.tripped and k.code == "LATCH_UNREADABLE"
    print("  KILL   -> a corrupt latch file is treated as TRIPPED, not as clear")


# --- arm ----------------------------------------------------------------------

def test_arm_expires_on_its_own():
    a = ArmStore()
    a.arm("closing", "dry", now=T0, ttl_s=600)
    assert a.current(now=T0 + 599) is not None
    assert a.current(now=T0 + 600) is None          # expiry is inclusive
    assert a.current(now=T0 + 1) is None            # and it is not resurrected
    print("  ARM    -> an arm expires by itself; no timer, no resurrection")


def test_arm_is_never_persisted():
    a = ArmStore()
    a.arm("closing", "live", now=T0, ttl_s=contract.ARM_TTL_S)
    assert ArmStore().current(now=T0 + 1) is None    # a "restarted" process
    print("  ARM    -> a fresh process comes up DISARMED, always")


def test_arm_names_one_strategy_and_one_mode():
    a = ArmStore()
    got = a.arm("closing", "dry", now=T0, ttl_s=60)
    assert (got.strategy, got.mode) == ("closing", "dry")
    assert got.remaining_s(T0 + 10) == 50
    try:
        a.arm("closing", "turbo", now=T0)
        raise AssertionError("unknown mode accepted")
    except ValueError:
        pass
    assert a.disarm() is True and a.current(now=T0) is None
    assert a.disarm() is False
    print("  ARM    -> one strategy, one known mode, disarm is idempotent")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
