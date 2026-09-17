"""Memory for the supervisor's last resort — the process self-restart.

17-Sep, seen live: Kite closed every WebSocket the feed opened ("Connection
closed: None - None") for over an hour of session time. The supervisor did
what 04-Aug taught it — two in-process restarts, then replace the process —
and then did it again three minutes later, and again, because `os.execv`
starts a fresh interpreter whose `starved_restarts` is zero. Each round
sent a phone push. That is the 84-push failure one level up.

This file is the memory `execv` cannot erase: when the last self-restart
happened and how many today. The verdict is a pure function so the policy
tests without a clock or a file; the supervisor persists a row before it
execs and reads it back after.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("tradewell.services")

STATE_PATH = Path(__file__).resolve().parents[1] / ".supervisor_state.json"
COOLDOWN_S = 900.0          # one self-restart per 15 minutes
MAX_PER_DAY = 3             # then it is not a socket problem the process can fix


def load(path: Optional[Path] = STATE_PATH) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except Exception as exc:  # pragma: no cover
        log.warning("supervisor state unreadable (%s) — starting empty", exc)
        return {}


def save(state: dict, path: Optional[Path] = STATE_PATH) -> None:
    if path is None:
        return
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(path)
        os.chmod(path, 0o600)
    except Exception as exc:  # pragma: no cover
        log.warning("supervisor state persist failed: %s", exc)


def verdict(state: dict, now: float, today: str) -> str:
    """'restart' | 'cooldown' | 'exhausted' — may the supervisor exec itself?"""
    if state.get("day") == today and int(state.get("count") or 0) >= MAX_PER_DAY:
        return "exhausted"
    last = state.get("last_at")
    if last is not None and now - float(last) < COOLDOWN_S:
        return "cooldown"
    return "restart"


def record(state: dict, now: float, today: str) -> dict:
    """The state to persist immediately BEFORE exec — a restart that fails
    to come back must still count, or the loop it prevents comes back."""
    count = int(state.get("count") or 0) if state.get("day") == today else 0
    return {"day": today, "count": count + 1, "last_at": float(now)}
