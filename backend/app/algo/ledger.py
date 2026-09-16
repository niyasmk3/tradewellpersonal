"""The audit trail — append-only JSONL, one line per thing that happened.

This file is the module's memory and its evidence. Every intent, every guard
verdict (including every block and its reason), every order sent, every broker
reply, every arm, disarm and kill lands here in the order it occurred and is
never rewritten. Three reasons it is append-only rather than a mutable store:

  * a post-mortem needs what was believed AT THE TIME, not the tidied version;
  * `day_state()` rebuilds the caps from this file, so a restart mid-session
    cannot hand the runner a fresh order budget — the ledger remembers;
  * if this account's automated orders are ever questioned, "here is every
    decision, in order, with the reason" is the only useful answer.

No DB, per the standing project decision. JSONL like signals/archive.py.

BURNED KEYS. An idempotency key is spent the moment an order is SENT, not when
it succeeds. A rejected order that auto-retries is how one bad decision becomes
forty; if a rejection deserves another attempt, that is a human's call.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

log = logging.getLogger("tradewell.algo")

_LEDGER_PATH = Path(__file__).resolve().parents[2] / ".algo_orders.jsonl"

# "order" is the WRITE-AHEAD row (an order is about to be sent); "result" is
# the broker's reply. They are separate rows because the file is append-only
# and because the key must be burned even if the process dies mid-send.
# "skip" is an adapter declining to emit (policy, no card, no quote) — logged
# so the dry-run ledger shows what it declined, not only what it fired.
EVENT_TYPES = ("intent", "verdict", "order", "result", "skip", "kill", "clear",
               "arm", "disarm", "reconcile")


@dataclass
class DayState:
    """The day-scoped counters the guard needs that only the ledger knows."""
    orders_today: int = 0
    open_orders_today: int = 0
    seen_keys: Set[str] = field(default_factory=set)


class AlgoLedger:
    def __init__(self, path: Optional[Path] = _LEDGER_PATH) -> None:
        self._lock = threading.Lock()
        self._path = path

    def record(self, type_: str, day: str, payload: dict,
               now: Optional[float] = None) -> dict:
        row = {"ts": time.time() if now is None else float(now),
               "type": type_, "day": day}
        row.update(payload)
        if self._path is None:
            return row
        with self._lock:
            try:
                new = not self._path.exists()
                with open(self._path, "a") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
                if new:
                    os.chmod(self._path, 0o600)
            except Exception as exc:  # pragma: no cover
                # A ledger write that fails is not cosmetic: the caps and the
                # duplicate defence are rebuilt from this file. Shout.
                log.error("ALGO LEDGER WRITE FAILED (%s): %s", type_, exc)
        return row

    def rows(self, day: Optional[str] = None) -> List[dict]:
        if self._path is None or not self._path.exists():
            return []
        out = []
        try:
            with open(self._path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue          # one corrupt line must not blind the rest
                    if day is None or r.get("day") == day:
                        out.append(r)
        except Exception as exc:  # pragma: no cover
            log.error("algo ledger unreadable: %s", exc)
        return out

    def tail(self, n: int = 200, day: Optional[str] = None) -> List[dict]:
        rows = self.rows(day)
        return rows[-n:]

    def day_state(self, day: str) -> DayState:
        """Rebuild today's counters from the file — survives a restart."""
        st = DayState()
        for r in self.rows(day):
            if r.get("type") != "order":
                continue
            st.orders_today += 1
            key = r.get("key")
            if key:
                st.seen_keys.add(key)
            if r.get("purpose") == "open":
                st.open_orders_today += 1
        return st

    def open_mode(self, key: Optional[str]) -> Optional[str]:
        """The mode an open was SENT in, by its key — any day. None if the
        ledger has no such open, which the guard treats as 'not ours'."""
        if not key:
            return None
        for r in self.rows():
            if r.get("type") == "order" and r.get("purpose") == "open" \
                    and r.get("key") == key:
                return r.get("mode")
        return None

    def open_position_keys(self, strategy: Optional[str] = None) -> List[dict]:
        """The runner's BELIEF about what it holds: every open whose broker
        reply was ok, minus every such open that an ok exit has since named
        in `closes`. This is the list reconcile.py will one day check against
        the broker's book; until then it is what the position cap counts."""
        acked: Set[str] = set()
        sent: Dict[str, dict] = {}
        closed: Set[str] = set()
        for r in self.rows():
            t = r.get("type")
            if t == "order" and r.get("key"):
                sent[r["key"]] = r
            elif t == "result" and r.get("ok") and r.get("key"):
                acked.add(r["key"])
        for key in acked:
            row = sent.get(key)
            if row is None:
                continue
            if row.get("purpose") == "open":
                continue
            if row.get("closes"):
                closed.add(row["closes"])
        out = []
        for key in acked:
            row = sent.get(key)
            if row and row.get("purpose") == "open" and key not in closed:
                if strategy is None or row.get("strategy") == strategy:
                    out.append(row)
        out.sort(key=lambda r: r.get("ts", 0))
        return out


algo_ledger = AlgoLedger()
