"""Signal stabilisation + history, keyed by (symbol, trading-mode).

The engine re-evaluates every few seconds, but a *signal* should stay put once
issued — same entry zone, stop, targets — until it expires, its trend flips, or
(Phase 3) it is hit/invalidated. The store enforces that: it holds one active
signal per (symbol, mode) and only adopts a fresh one when the slot is free.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from app.signals.models import (
    Action,
    Bias,
    Direction,
    SignalCard,
    SignalResponse,
    SignalState,
    TradingMode,
)

log = logging.getLogger("tradewell.signals")

_HISTORY_MAX = 50
# Signals are the product's core output — persist active + history so a backend
# restart (or dev --reload) doesn't erase the day's record. Backend-anchored
# like the other persistence files.
_STORE_PATH = Path(__file__).resolve().parents[2] / ".signals.json"


def _key(symbol: str, mode: TradingMode) -> str:
    return f"{symbol.upper()}:{mode.value}"


def _ist_day(ts: int) -> int:
    return (ts + 19800) // 86400


@dataclass
class ThrottleConfig:
    """How often the engine is ALLOWED to speak, independent of score."""
    max_per_day: int = 4
    min_gap_s: int = 900
    cooldown_s: int = 600
    flip_guard_s: int = 1800
    max_consecutive_losses: int = 2
    daily_loss_limit: float = 0.0        # rupees, positive; 0 = disabled
    # Guards that act on positions still OPEN. The realised-only breakers above
    # cannot fire while you are holding losers, which is exactly when the engine
    # must stop talking — see RiskState.open_pnl.
    max_open_positions: int = 2          # 0 = disabled
    max_open_drawdown: float = 0.0       # rupees, positive; 0 = disabled


@dataclass
class RiskState:
    """Live trading outcome, supplied by the caller (the store must not reach
    into the trade journal itself)."""
    consecutive_losses: int = 0
    realized_today: float = 0.0
    # Mark-to-market on positions still open, and how many there are.
    # `open_pnl` is signed: negative means you are currently down.
    open_pnl: float = 0.0
    open_positions: int = 0


@dataclass
class _SlotState:
    day: int = 0
    issued: int = 0
    last_issued_at: int = 0
    last_retired_at: int = 0
    last_direction: str = ""


class SignalStore:
    def __init__(self, store_path: Path | None = None) -> None:
        """store_path=None -> memory-only (unit tests / ad-hoc stores).

        Only the module singleton below persists. A test store MUST NOT write
        here: synthetic cards would land in the live history, and a fixture left
        'active' with a future valid_until would be served as a REAL signal
        after the next restart.
        """
        self._lock = threading.Lock()
        self._active: dict[str, SignalCard] = {}
        self._history: dict[str, list[SignalCard]] = {}
        self._latest: dict[str, SignalResponse] = {}
        self._slots: dict[str, _SlotState] = {}
        self._store_path = store_path
        self._load()

    # ---- throttle ----------------------------------------------------------
    def _slot(self, key: str, now: int) -> _SlotState:
        s = self._slots.get(key)
        day = _ist_day(now)
        if s is None or s.day != day:      # counters reset with the IST day
            s = _SlotState(day=day)
            self._slots[key] = s
        return s

    def _blocked(
        self, key: str, card: SignalCard, now: int,
        cfg: ThrottleConfig, risk: RiskState,
    ) -> str | None:
        """Reason this otherwise-valid card must NOT be issued, or None.

        A tradeable score says the setup looks good; these say the engine has
        already spoken enough, or that today has gone badly enough to stop.
        """
        # Circuit breakers first — they outrank everything.
        if risk.consecutive_losses >= cfg.max_consecutive_losses:
            return (f"Circuit breaker — {risk.consecutive_losses} losing trades in a row. "
                    "Signals paused for today.")

        # OPEN-position guards. These exist because the realised breakers below
        # are blind exactly when it matters: holding losers means nothing is
        # realised, so the engine happily issues signal after signal into a
        # position already deep in drawdown. On 20-Jul-2026 that produced six
        # PE signals in 66 minutes while three PE positions sat ~₹32,000 down.
        if cfg.max_open_positions > 0 and risk.open_positions >= cfg.max_open_positions:
            return (f"{risk.open_positions} position(s) already open — close or reduce "
                    "before taking another. Stacking correlated bets is what turns a "
                    "bad call into a bad day.")
        if cfg.max_open_drawdown > 0 and risk.open_pnl <= -abs(cfg.max_open_drawdown):
            return (f"Open positions are down ₹{abs(risk.open_pnl):,.0f} — at or beyond the "
                    "open-drawdown limit. Signals paused until that is resolved.")

        # The daily loss limit counts UNREALISED damage too: a loss you are
        # still holding is not a smaller loss than one you have booked.
        exposure = risk.realized_today + min(0.0, risk.open_pnl)
        if cfg.daily_loss_limit > 0 and exposure <= -abs(cfg.daily_loss_limit):
            held = f" (₹{abs(risk.open_pnl):,.0f} of it still open)" if risk.open_pnl < 0 else ""
            return (f"Daily loss limit hit (₹{abs(exposure):,.0f}){held}. "
                    "Signals paused for today.")

        s = self._slot(key, now)
        if s.issued >= cfg.max_per_day:
            return f"Daily signal limit reached ({cfg.max_per_day}) — no more setups today."
        if s.last_issued_at and now - s.last_issued_at < cfg.min_gap_s:
            wait = cfg.min_gap_s - (now - s.last_issued_at)
            return f"Throttled — {wait // 60}m until the next signal may be issued."
        if s.last_retired_at and now - s.last_retired_at < cfg.cooldown_s:
            wait = cfg.cooldown_s - (now - s.last_retired_at)
            return f"Cooling down after the last setup — {wait // 60}m remaining."
        if (
            s.last_direction
            and card.direction.value != s.last_direction
            and s.last_issued_at
            and now - s.last_issued_at < cfg.flip_guard_s
        ):
            return (f"Direction flip blocked — last signal was {s.last_direction} "
                    f"{(now - s.last_issued_at) // 60}m ago. Waiting for the tape to settle.")
        return None

    def _load(self) -> None:
        if self._store_path is None:
            return
        try:
            if not self._store_path.exists():
                return
            data = json.loads(self._store_path.read_text())
            # Only today's cards are meaningful: an entry zone / stop priced off
            # a previous session must never resurface as a live signal after a
            # restart, however fresh its valid_until looks.
            import time as _t
            today = (int(_t.time()) + 19800) // 86400
            same_day = lambda c: (c.created_at + 19800) // 86400 == today
            for key, entry in data.items():
                if entry.get("active"):
                    card = SignalCard.model_validate(entry["active"])
                    if same_day(card):
                        self._active[key] = card
                self._history[key] = [
                    c for c in (SignalCard.model_validate(x) for x in entry.get("history", []))
                    if same_day(c)
                ][:_HISTORY_MAX]
        except Exception as exc:  # corrupt cache: live evaluation rebuilds state
            log.warning("signal store cache unreadable, starting fresh: %s", exc)

    def _save_locked(self) -> None:
        if self._store_path is None:
            return
        try:
            payload = {
                key: {
                    "active": self._active[key].model_dump(mode="json") if key in self._active else None,
                    "history": [c.model_dump(mode="json") for c in self._history.get(key, [])],
                }
                for key in set(self._active) | set(self._history)
            }
            tmp = self._store_path.with_name(self._store_path.name + ".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._store_path)
        except Exception as exc:  # pragma: no cover
            log.warning("signal store persist failed: %s", exc)

    def reconcile(
        self,
        fresh: SignalResponse,
        now: int,
        cfg: ThrottleConfig | None = None,
        risk: RiskState | None = None,
    ) -> SignalResponse:
        cfg = cfg or ThrottleConfig()
        risk = risk or RiskState()
        key = _key(fresh.symbol, fresh.mode)
        with self._lock:
            active = self._active.get(key)
            dirty = False
            blocked: str | None = None

            if active is not None:
                if now >= active.valid_until:
                    self._retire(key, active, SignalState.EXPIRED, now)
                    active = None
                    dirty = True
                elif self._trend_flipped(fresh, active):
                    self._retire(key, active, SignalState.CANCELLED, now)
                    active = None
                    dirty = True

            # Adopt a fresh signal only when no active one holds the slot AND
            # the throttle allows the engine to speak again.
            if active is None and fresh.signal is not None:
                blocked = self._blocked(key, fresh.signal, now, cfg, risk)
                if blocked is None:
                    active = fresh.signal
                    self._active[key] = active
                    self._history.setdefault(key, []).insert(0, active)
                    del self._history[key][_HISTORY_MAX:]
                    slot = self._slot(key, now)
                    slot.issued += 1
                    slot.last_issued_at = now
                    slot.last_direction = active.direction.value
                    dirty = True
                else:
                    log.info("signal suppressed (%s): %s", key, blocked)

            final = fresh.model_copy(deep=True)
            if active is not None:
                final.signal = active
                final.action = active.action
                final.no_trade_reason = None
            else:
                final.signal = None
                if blocked:
                    # Show WHY a qualifying setup was withheld — silence would
                    # look like the engine simply found nothing.
                    final.action = Action.AVOID
                    final.no_trade_reason = blocked
            self._latest[key] = final
            if dirty:
                self._save_locked()  # few writes/day: adopt + retire transitions only
            return final

    @staticmethod
    def _trend_flipped(fresh: SignalResponse, active: SignalCard) -> bool:
        opposite = Bias.BEARISH if active.direction is Direction.CE else Bias.BULLISH
        return fresh.status.bias is opposite

    def _retire(self, key: str, card: SignalCard, state: SignalState, now: int = 0) -> None:
        card.state = state  # history holds the same object reference, so this updates it too
        self._active.pop(key, None)
        if now:
            self._slot(key, now).last_retired_at = now  # starts the cooldown

    def latest(self, symbol: str, mode: TradingMode) -> SignalResponse | None:
        # Deep-copy: the stored response references the live active card, whose
        # .state the eval loop may mutate (_retire) while this is being serialised.
        with self._lock:
            resp = self._latest.get(_key(symbol, mode))
            return resp.model_copy(deep=True) if resp is not None else None

    def history(self, symbol: str, mode: TradingMode) -> list[SignalCard]:
        with self._lock:
            return [c.model_copy(deep=True) for c in self._history.get(_key(symbol, mode), [])]


signal_store = SignalStore(store_path=_STORE_PATH)
