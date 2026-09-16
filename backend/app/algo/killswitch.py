"""The kill switch — a latch that a restart cannot clear.

SEMANTICS, stated once and loudly: a tripped switch stops the runner emitting
ANY order, including exits. That is deliberate and it is the uncomfortable
choice. The switch trips when the system's model of the world has been shown
to be wrong — an order we did not place, a fill we did not expect, a position
that vanished, three rejections in a row — and "keep placing orders while your
model of the account is known-wrong" is how a small fault becomes a large one.
So the switch hands the account back to the human, and pages them. An open
position at that moment is the human's to close, in Kite, by hand.

PERSISTED ON PURPOSE. The first instinct is to hold this in memory; that
instinct is wrong. "Restart it and the error goes away" is precisely how a
broken algo gets a second attempt at the same mistake. The latch lives in a
file, it is read at construction, and only an explicit human clear() removes
it — which is itself logged.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("tradewell.algo")

_STORE_PATH = Path(__file__).resolve().parents[2] / ".algo_killswitch.json"


class KillSwitch:
    def __init__(self, path: Optional[Path] = _STORE_PATH) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._state: dict = {}
        self._load()

    # ---- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            if data.get("tripped_at"):
                self._state = data
                log.warning("algo kill switch is TRIPPED on load: %s — %s",
                            data.get("code"), data.get("detail"))
        except Exception as exc:  # pragma: no cover
            # An unreadable latch file must read as TRIPPED, never as clear:
            # the one interpretation we cannot afford is "corrupt, so carry on".
            self._state = {"tripped_at": time.time(), "code": "LATCH_UNREADABLE",
                           "detail": str(exc)}
            log.error("algo kill-switch file unreadable — treating as tripped: %s", exc)

    def _save_locked(self) -> None:
        if self._path is None:
            return
        try:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(self._state, indent=2))
            tmp.replace(self._path)
            os.chmod(self._path, 0o600)
        except Exception as exc:  # pragma: no cover
            log.error("algo kill-switch persist FAILED: %s", exc)

    # ---- api -----------------------------------------------------------------

    @property
    def tripped(self) -> bool:
        with self._lock:
            return bool(self._state.get("tripped_at"))

    @property
    def code(self) -> Optional[str]:
        with self._lock:
            return self._state.get("code") if self._state.get("tripped_at") else None

    def trip(self, code: str, detail: str = "", now: Optional[float] = None) -> bool:
        """Latch. Returns True only for a NEW trip, so the pager fires once.

        A second trip while already tripped does not overwrite the first: the
        original cause is the one worth reading, and a cascade of consequent
        faults must not bury it.
        """
        with self._lock:
            if self._state.get("tripped_at"):
                return False
            self._state = {"tripped_at": time.time() if now is None else float(now),
                           "code": code, "detail": detail}
            self._save_locked()
        log.error("ALGO KILL SWITCH TRIPPED: %s — %s", code, detail)
        return True

    def clear(self, by: str, now: Optional[float] = None) -> bool:
        """Human action only. Returns True if something was actually cleared."""
        with self._lock:
            if not self._state.get("tripped_at"):
                return False
            prev = dict(self._state)
            self._state = {"cleared_at": time.time() if now is None else float(now), "cleared_by": by,
                           "previous": prev}
            self._save_locked()
        log.warning("algo kill switch cleared by %s (was %s)", by, prev.get("code"))
        return True

    def state(self) -> dict:
        with self._lock:
            s = dict(self._state)
        s["tripped"] = bool(s.get("tripped_at"))
        return s


kill_switch = KillSwitch()
