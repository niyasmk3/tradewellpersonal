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
from typing import Callable

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
    """How often the engine is ALLOWED to speak, independent of score.

    Pure CADENCE shaping — spacing and per-day count. The outcome-based circuit
    breakers (losing streak, daily loss, open-position and open-drawdown halts)
    were removed 25-Jul at the user's instruction: this is a personal advisory
    tool that places no orders, and the trader chose to keep the engine talking
    even through a bad run rather than have it fall silent. The realised loss is
    still theirs to cap with the stop they act on.
    """
    max_per_day: int = 4
    min_gap_s: int = 900
    cooldown_s: int = 600
    flip_guard_s: int = 1800


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
        # Off-desk delivery hooks, wired by the live feed only (services.py);
        # None everywhere else so tests and offline replays stay silent.
        # notify: a newly ADOPTED card. notify_retire: (card, state) when an
        # active card dies — a thesis that flips or expires deserves a phone
        # buzz just as much as one that is born.
        self.notify: Callable[[SignalCard], bool] | None = None
        self.notify_retire = None
        # Append-only history sink (signals/archive.py). Attached to the module
        # SINGLETON only — this store is deliberately day-scoped, so without
        # the archive every restart erased yesterday from the history tab.
        # None everywhere else: tests must not write synthetic cards there.
        self.archive = None
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
        self, key: str, card: SignalCard, now: int, cfg: ThrottleConfig,
    ) -> str | None:
        """Reason this otherwise-valid card must NOT be issued, or None.

        A tradeable score says the setup looks good; these say the engine has
        already spoken enough recently. This is CADENCE only — the outcome
        breakers that used to lead here (losing streak / daily loss / open
        positions / open drawdown) were removed 25-Jul at the user's request.
        """
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
    ) -> SignalResponse:
        cfg = cfg or ThrottleConfig()
        key = _key(fresh.symbol, fresh.mode)
        adopted: SignalCard | None = None     # pushed after the lock is released
        retired: tuple[SignalCard, str] | None = None   # likewise
        with self._lock:
            active = self._active.get(key)
            dirty = False
            blocked: str | None = None

            if active is not None:
                if now >= active.valid_until:
                    self._retire(key, active, SignalState.EXPIRED, now)
                    retired = (active, "expired")
                    active = None
                    dirty = True
                elif self._trend_flipped(fresh, active):
                    self._retire(key, active, SignalState.CANCELLED, now)
                    retired = (active, "cancelled")
                    active = None
                    dirty = True

            # Adopt a fresh signal only when no active one holds the slot AND
            # the throttle allows the engine to speak again.
            if active is None and fresh.signal is not None:
                blocked = self._blocked(key, fresh.signal, now, cfg)
                if blocked is None:
                    active = fresh.signal
                    adopted = active
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

        # Outside the lock on purpose: the push dispatches a thread, but even
        # that much work does not belong under a lock the evaluation loop takes
        # every few seconds. A failure here is logged and ignored — delivery is
        # best-effort, the card is already issued either way.
        #
        # INJECTED, never resolved here. An earlier version called
        # get_settings() + push_signal directly, which read the developer's
        # real .env inside every test that adopts a card — a webhook configured
        # there would have made the test suite send live alerts. Only the live
        # feed (services.py) wires `notify`; everything else adopts silently.
        if adopted is not None and self.notify is not None:
            try:
                if self.notify(adopted):
                    log.info("signal pushed (%s): %s score %.1f",
                             key, adopted.contract, adopted.confidence or 0)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("signal push failed: %s", exc)
        if retired is not None and self.notify_retire is not None:
            try:
                self.notify_retire(retired[0], retired[1])
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("retire push failed: %s", exc)
        # Permanent history: adoption snapshot, then the retirement supersedes
        # it (readers keep the last line per id). record() never raises.
        if self.archive is not None:
            if adopted is not None:
                self.archive.record(adopted)
            if retired is not None:
                self.archive.record(retired[0])
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

    def close_active(self, symbol: str, mode: TradingMode, now: int, reason: str) -> bool:
        """Retire the active card and blank the served response.

        Used by refresh when the live score no longer qualifies: the signal must
        not merely be re-priced, it must go away — an orderable card for a dead
        thesis is exactly what refresh exists to prevent. Returns True if a card
        was actually closed.
        """
        key = _key(symbol, mode)
        with self._lock:
            card = self._active.get(key)
            if card is None:
                return False
            self._retire(key, card, SignalState.CANCELLED, now)
            resp = self._latest.get(key)
            if resp is not None:
                resp.signal = None
                resp.action = Action.AVOID
                resp.no_trade_reason = reason
                resp.evaluated_at = now
            self._save_locked()
        if self.archive is not None:      # outside the lock, like the pushes
            self.archive.record(card)
        return True

    def reprice_active(
        self, symbol: str, mode: TradingMode, new_entry: float, ladder: dict, now: int,
        spot: float | None = None,
    ) -> SignalCard | None:
        """Re-price the ACTIVE card's premium levels against `new_entry`.

        Keeps the card's identity, strike, direction and index invalidation —
        it is the same trade at today's price. Only the premium-derived ladder
        (entry zone, stop, targets, backstop, early-partial) and the freshness
        timestamps change. Returns the refreshed card, or None if there is no
        active card to re-price.

        Deliberately does NOT touch the daily throttle counters: refreshing a
        card the user is already looking at is not a new signal, so it must not
        consume the day's signal budget.
        """
        key = _key(symbol, mode)
        with self._lock:
            card = self._active.get(key)
            if card is None:
                return None
            # Audit trail BEFORE overwriting: the ladder the user was looking
            # at (and may have executed against) must survive the refresh.
            _rh = card.reprice_history or []
            # Slot 0 is the ISSUE-TIME ladder and is NEVER rotated out — the
            # only durable copy of the card's first values under unlimited
            # refreshes (04-Aug: 27 reprices in 9 min rotated the birth ladder
            # away and history presented a 30.7 zone as the 14.5 issue-time
            # offer the paper book had actually filled).
            card.reprice_history = (([_rh[0]] + _rh[1:][-8:]) if _rh else []) + [{
                "at": now, "ref": card.ref_entry_premium, "ref_spot": card.ref_spot,
                "premium_sl": card.premium_sl, "target1": card.target1,
                "target2": card.target2, "entry_low": card.entry_low,
                "entry_high": card.entry_high,
            }]
            card.reprice_total = (card.reprice_total or 0) + 1
            card.entry_low = ladder["entry_low"]
            card.entry_high = ladder["entry_high"]
            card.premium_sl = ladder["premium_sl"]
            card.disaster_sl = ladder["disaster_sl"]
            card.quick_target = ladder["quick_target"]
            card.target1 = ladder["target1"]
            card.target2 = ladder["target2"]
            card.trailing_sl_rule = ladder["trailing_sl_rule"]
            card.risk_reward = ladder["risk_reward"]
            card.ref_entry_premium = new_entry
            if spot is not None and spot > 0:
                # A refresh that keeps a stale ref_spot re-prices the premium
                # against one market and the invalidation against another —
                # that mismatch produced a 3-minute hair-trigger exit on 23-Jul.
                card.ref_spot = spot
            # created_at is the card's BIRTH and never moves; freshness guards
            # (Kite hand-off, UI age) read repriced_at instead. The entry
            # window still extends from now — the re-priced zone is fresh.
            span = card.valid_until - max(card.repriced_at or card.created_at, card.created_at)
            card.repriced_at = now
            card.valid_until = now + max(span, 0)
            # The _latest response references this same object, but refresh its
            # evaluated_at so the UI shows the card as just-updated.
            resp = self._latest.get(key)
            if resp is not None:
                resp.evaluated_at = now
            self._save_locked()
            snapshot = card.model_copy(deep=True)
        # Archive the repriced ladder (outside the lock, like the other
        # events): without this, a process that dies before the card's
        # natural retirement leaves the archive holding the STALE pre-reprice
        # levels forever — the day-scoped store file won't survive to correct
        # it after the IST rollover.
        if self.archive is not None:
            self.archive.record(snapshot)
        return snapshot

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
from app.signals.archive import signal_archive as _signal_archive  # noqa: E402
signal_store.archive = _signal_archive

# SHADOW STORES for vetoed cards — setups a measured-hypothesis gate withheld
# from the live feed. They get the same stabilisation (one active per slot,
# throttle, day-scoped persistence) so the paper book can fill them as tagged
# counterfactuals, but they are never served to the dashboard, never pushed,
# and never enterable. Slot counters are their own: a shadow card must not
# consume the real feed's daily quota, and vice versa.
#
# ONE STORE PER HYPOTHESIS CLASS (review catch, 03-Aug): a SignalStore holds
# one active card per symbol+mode, and with every class sharing a single
# store, a squatting floor shadow silently DROPPED refire/late candidates —
# same-direction candidates never displace an incumbent, so whichever class
# adopted first throttled the other ledgers' 30-fill verdicts with no record
# anywhere. The class identity itself rides each card's hollow_reason, so
# consumers (paper tags, shadow_class) are unaffected by which file a card
# came from.
hollow_store = SignalStore(store_path=_STORE_PATH.with_name(".hollow_signals.json"))
late_shadow_store = SignalStore(store_path=_STORE_PATH.with_name(".late_signals.json"))
refire_shadow_store = SignalStore(store_path=_STORE_PATH.with_name(".refire_signals.json"))
setup_shadow_store = SignalStore(store_path=_STORE_PATH.with_name(".setup_signals.json"))
confirm_shadow_store = SignalStore(store_path=_STORE_PATH.with_name(".confirm_signals.json"))

# Shadow cards get their OWN append-only archive (04-Aug find: the four shadow
# stores are day-scoped and had archive=None, so 7 of the day's 10 cards left
# no permanent record — the hypothesis ledgers' own evidence was self-erasing
# at every restart). Separate file on purpose: consumers of the live archive
# (history tab, report cards) must never see counterfactual cards.
from app.signals.archive import shadow_signal_archive as _shadow_archive  # noqa: E402
for _s in (hollow_store, late_shadow_store, refire_shadow_store, setup_shadow_store,
           confirm_shadow_store):
    _s.archive = _shadow_archive


def shadow_store_for(tag: str) -> SignalStore:
    """Route a shadow candidate to its hypothesis class's own store.

    `tag` is the shadow_tag the veto resolution produced: "late: ...",
    "refire: ...", "setup: ..." (P1-4 detectors), or the participation-floor
    reason verbatim (no prefix).
    """
    if tag.startswith("late:"):
        return late_shadow_store
    if tag.startswith("refire:"):
        return refire_shadow_store
    if tag.startswith("setup:"):
        return setup_shadow_store
    if tag.startswith("confirm:"):
        return confirm_shadow_store
    return hollow_store
