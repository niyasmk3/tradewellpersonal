"""Process-level self-restart — the one recovery the in-process paths can't do.

Two failure modes only a FRESH PROCESS fixes, both lived through:
  * a dead KiteTicker — its Twisted reactor cannot be restarted in-process
    (20-Jul: "restart feed" reconnected everything except the socket);
  * a process started before a deploy, running old code forever (27-Jul:
    Monday open arrived with Friday-evening's broken snapshot still serving).

os.execv replaces this process IN PLACE: same PID, so caffeinate (the sleep
blocker) and start.sh's `wait` are undisturbed; the listening socket closes on
exec (Python FDs are non-inheritable) and the fresh interpreter rebinds it,
re-imports the code now on disk, reloads the same-day Kite token from
.kite_session.json and auto-starts the feed. If the token has expired, the
dashboard shows the login gate — exactly what a terminal restart would do.

Advisory-tool posture unchanged: restarting places, modifies, closes nothing.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter

log = logging.getLogger("tradewell.system")

router = APIRouter(prefix="/system", tags=["system"])

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _relaunch_argv() -> list[str]:
    """The exec target: this interpreter, `-m uvicorn`, the original args.

    Under `python -m uvicorn app.main:app --host … --port …`, sys.argv is
    ['/…/uvicorn/__main__.py', 'app.main:app', '--host', …] — argv[1:] is the
    launch spec verbatim. If argv doesn't look like our uvicorn launch (some
    exotic invocation), fall back to start.sh's exact command line.
    """
    args = sys.argv[1:]
    if not any(a.startswith("app.main") for a in args):
        args = ["app.main:app", "--host", "127.0.0.1", "--port", "8000"]
    return [sys.executable, "-m", "uvicorn", *args]


def _exec_self(argv: list[str], delay_s: float = 0.8) -> None:  # pragma: no cover
    """Sleep long enough for the HTTP response to leave, then become the new
    process. Never returns on success; any failure is logged, not raised — a
    failed restart must leave the current process running, not dead."""
    try:
        time.sleep(delay_s)
        os.chdir(str(_BACKEND_DIR))       # uvicorn resolves app.main from cwd
        log.warning("self-restart: exec %s", " ".join(argv))
        os.execv(argv[0], argv)
    except Exception:
        log.exception("self-restart failed — process continues unchanged")


@router.post("/restart")
def restart_backend() -> dict:
    """Replace the backend process with a fresh one running the code on disk.

    Responds first, execs ~0.8s later from a daemon thread (execv from a
    non-main thread replaces the whole process on POSIX). The frontend keeps
    running and its polls recover once the new process binds the port.
    """
    argv = _relaunch_argv()
    threading.Thread(target=_exec_self, args=(argv,), daemon=True,
                     name="tradewell-restart").start()
    return {
        "restarting": True,
        "note": ("Backend replacing itself — back in ~10-20s on the current "
                 "code. Same-day Kite token is reloaded; if it expired you "
                 "will see the login gate."),
    }
