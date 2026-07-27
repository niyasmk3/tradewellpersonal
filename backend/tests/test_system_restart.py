"""Self-restart endpoint tests — the exec is mocked; nothing actually restarts.

Run:  python backend/tests/test_system_restart.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import routes_system as rs


def test_relaunch_argv_reconstructs_uvicorn_launch():
    orig = sys.argv
    try:
        # The start.sh shape: python -m uvicorn app.main:app --host … --port …
        sys.argv = ["/x/uvicorn/__main__.py", "app.main:app",
                    "--host", "127.0.0.1", "--port", "8000"]
        argv = rs._relaunch_argv()
        assert argv[0] == sys.executable
        assert argv[1:3] == ["-m", "uvicorn"]
        assert argv[3:] == ["app.main:app", "--host", "127.0.0.1", "--port", "8000"]
        # An exotic invocation falls back to start.sh's exact command line.
        sys.argv = ["something-weird"]
        argv = rs._relaunch_argv()
        assert argv[3:] == ["app.main:app", "--host", "127.0.0.1", "--port", "8000"]
    finally:
        sys.argv = orig
    print("  ARGV   -> uvicorn launch reconstructed; weird argv falls back safely")


def test_restart_endpoint_schedules_and_responds():
    """The endpoint must RESPOND (restarting: true) and schedule the exec on a
    daemon thread — never exec inline, or the caller gets no answer."""
    scheduled = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            scheduled["target"] = target
            scheduled["args"] = args
            scheduled["daemon"] = daemon

        def start(self):
            scheduled["started"] = True

    orig = rs.threading.Thread
    rs.threading.Thread = _FakeThread
    try:
        out = rs.restart_backend()
    finally:
        rs.threading.Thread = orig
    assert out["restarting"] is True
    assert scheduled["started"] and scheduled["daemon"] is True
    assert scheduled["target"] is rs._exec_self
    argv = scheduled["args"][0]
    assert argv[0] == sys.executable and "-m" in argv
    print("  SCHED  -> responds first, execs from a daemon thread")


def test_failed_exec_leaves_the_process_alive():
    """The non-negotiable: _exec_self must swallow exec failures — a broken
    relaunch target has to leave the CURRENT backend running, not dead. The
    happy path (same PID, socket rebound, fresh import) was proven live on a
    throwaway port 8055 during development; what the suite can safely pin is
    the failure side of the irreversible line."""
    rs._exec_self(["/nonexistent/interpreter", "-m", "uvicorn"], delay_s=0.0)
    # Reaching this line IS the assertion: execv raised, was caught, we live.
    print("  SAFE   -> a failed exec logs and returns; the process survives")


def test_boot_id_is_stable_within_a_process():
    """The dashboard reloads only when this CHANGES — so within one process it
    must never change, and it must exist."""
    a = rs.boot()
    b = rs.boot()
    assert a["boot_id"] and a["boot_id"] == b["boot_id"]
    print("  BOOTID -> stable within a process; restart response carries it")


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
