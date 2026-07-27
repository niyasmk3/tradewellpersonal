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
import uuid
from pathlib import Path

from fastapi import APIRouter

log = logging.getLogger("tradewell.system")

router = APIRouter(prefix="/system", tags=["system"])

_BACKEND_DIR = Path(__file__).resolve().parents[2]

# Stamped at IMPORT — a fresh interpreter gets a fresh id even though execv
# keeps the PID. This is the only reliable "did the restart actually happen"
# signal: same PID by design, and /auth/status looks identical either way. The
# dashboard reloads only after seeing a DIFFERENT id, so a silently failed
# exec can never masquerade as a successful restart.
_BOOT_ID = uuid.uuid4().hex[:12]


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


@router.get("/boot")
def boot() -> dict:
    """This process generation's identity — changes only on a real restart."""
    return {"boot_id": _BOOT_ID}


@router.post("/restart")
def restart_backend() -> dict:
    """Replace the backend process with a fresh one running the code on disk.

    Responds first, execs ~0.8s later from a daemon thread (execv from a
    non-main thread replaces the whole process on POSIX). The frontend keeps
    running and reloads only once /system/boot reports a NEW boot id.

    Timing honesty: the fresh process does not bind the port until its lifespan
    finishes, and the lifespan awaits the feed start — including up to a dozen
    sequential Kite historical fetches for candle re-seeding. Typical is
    10-30s; a degraded Kite/network day (exactly when this button gets used)
    can take a couple of minutes. The caller's deadline must budget for that.
    """
    argv = _relaunch_argv()
    threading.Thread(target=_exec_self, args=(argv,), daemon=True,
                     name="tradewell-restart").start()
    return {
        "restarting": True,
        "boot_id": _BOOT_ID,
        "note": ("Backend replacing itself — typically back in 10-30s (longer "
                 "if Kite data re-seeding is slow). Same-day Kite token is "
                 "reloaded; if it expired you will see the login gate."),
    }
