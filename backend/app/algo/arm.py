"""Arm state — the deliberate, expiring, in-memory permission to trade.

NOT PERSISTED, and that is the feature. An arm that survives a restart means a
process can come up trading without a human in the room; the only safe default
for a fresh process is disarmed. Restarting therefore always de-arms, and a
human re-arms with their eyes on the screen.

Three properties the UI depends on:
  * an arm names ONE strategy — arming the closing rule must not silently
    authorise the gold loop;
  * an arm names ONE mode — "dry" and "paper" are free, "live" is not;
  * an arm EXPIRES on its own (contract.ARM_TTL_S), so the failure mode
    "armed live on Friday, forgot, Monday happens" cannot occur.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.algo import contract

log = logging.getLogger("tradewell.algo")


@dataclass(frozen=True)
class Arm:
    strategy: str
    mode: str                   # "dry" | "paper" | "live"
    armed_at: float
    expires_at: float
    by: str = "ui"

    def remaining_s(self, now: float) -> float:
        return max(0.0, self.expires_at - now)


class ArmStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._arm: Optional[Arm] = None

    def arm(self, strategy: str, mode: str, by: str = "ui",
            now: Optional[float] = None, ttl_s: Optional[float] = None) -> Arm:
        if mode not in contract.MODES:
            raise ValueError("unknown mode: %s" % mode)
        t = time.time() if now is None else float(now)
        ttl = float(ttl_s if ttl_s is not None else contract.ARM_TTL_S)
        a = Arm(strategy=strategy, mode=mode, armed_at=t, expires_at=t + ttl, by=by)
        with self._lock:
            self._arm = a
        log.warning("algo ARMED: strategy=%s mode=%s ttl=%.0fs by=%s",
                    strategy, mode, ttl, by)
        return a

    def disarm(self, by: str = "ui") -> bool:
        with self._lock:
            had = self._arm is not None
            self._arm = None
        if had:
            log.warning("algo DISARMED by %s", by)
        return had

    def current(self, now: Optional[float] = None) -> Optional[Arm]:
        """The live arm, or None — expiry is evaluated here, not by a timer."""
        t = time.time() if now is None else float(now)
        with self._lock:
            a = self._arm
            if a is not None and a.expires_at <= t:
                self._arm = None
                a = None
        return a


arm_store = ArmStore()
