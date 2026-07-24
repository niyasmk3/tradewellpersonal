"""Dead-feed watchdog — pages the phone when ticks stop during market hours.

LIVES OUTSIDE THE FEED LIFECYCLE ON PURPOSE. The first version ran as a feed
task, which meant the supervisor's recovery restart (or a failed restart with a
dead token) cancelled the watchdog at exactly the moment its job began: one
page, then hours of unmonitored blackout — a rebuild of the 23-Jul failure with
extra steps. This one is started once at app startup, next to the supervisor,
and keeps checking whether the feed object even exists.

The decision logic is a small synchronous state machine (`FeedWatchdog.check`)
so it can be unit-tested without an event loop, a clock, or a network:

  * GRACE, not hair-trigger: silence is measured as time since the newer of
    (last usable tick, session-relevant baseline). A fresh process still
    connecting, or ticks left over from yesterday's session, must get the full
    configured window before the first page — a pager that cries wolf at 09:15
    trains its owner to ignore the one page that matters.
  * CROSS-EPISODE COOLDOWN: at most one page per 10 minutes, no matter how the
    starvation episodes are sliced. A half-broken socket delivering a tick
    every ~3 minutes must not produce a page/recovery storm.
  * HYSTERESIS on recovery: the all-clear needs several consecutive healthy
    checks, and is only sent if this episode actually paged.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("tradewell.watchdog")

_PAGE_SPACING_S = 600        # min seconds between pages, across episodes
_RECOVERY_STREAK = 3         # consecutive healthy checks before the all-clear


class FeedWatchdog:
    def __init__(self) -> None:
        self._baseline: float | None = None
        self._prev_open = False
        self._starved_since: float | None = None
        self._last_page = 0.0
        self._paged_this_episode = False
        self._healthy_streak = 0

    def _reset_episode(self) -> None:
        self._starved_since = None
        self._paged_this_episode = False
        self._healthy_streak = 0

    def check(
        self, now: float, age: float | None, market_open: bool, limit: int
    ) -> str | None:
        """One observation → "page", "recover", or None.

        `age` is seconds since the newest tick (None = no tick this process).
        """
        if limit <= 0 or not market_open:
            self._prev_open = market_open
            self._baseline = None
            self._reset_episode()
            return None

        # Session (or watchdog) just came alive: the grace clock starts NOW.
        # Yesterday's ticks in state make `age` hours old at 09:15 — that is
        # not today's silence, and must not be paged as if it were.
        if not self._prev_open or self._baseline is None:
            self._baseline = now
        self._prev_open = True

        observed = now - self._baseline
        silence = observed if age is None else min(age, observed)

        if silence > limit:
            self._healthy_streak = 0
            if self._starved_since is None:
                self._starved_since = now
            if now - self._last_page >= _PAGE_SPACING_S or self._last_page == 0.0:
                self._last_page = now
                self._paged_this_episode = True
                return "page"
            return None

        if self._starved_since is not None:
            self._healthy_streak += 1
            if self._healthy_streak >= _RECOVERY_STREAK:
                paged = self._paged_this_episode
                self._reset_episode()
                return "recover" if paged else None
        return None


async def watchdog_loop(interval: float = 30.0) -> None:
    """Drive the state machine against live state; page via the webhook."""
    from app.config import get_settings
    from app.market import calendar as mcal
    from app.notify import push_text
    from app.state import market_state

    wd = FeedWatchdog()
    while True:
        await asyncio.sleep(interval)
        try:
            cfg = get_settings()
            age = market_state.last_tick_age()
            action = wd.check(time.time(), age, mcal.is_market_open(),
                              cfg.feed_watchdog_age_s)
            if action == "page":
                push_text(
                    "TRADEWELL FEED SILENT",
                    f"No usable ticks for over {cfg.feed_watchdog_age_s}s during market "
                    "hours — the engine is BLIND. Check the start.sh terminal and the "
                    "Kite login.",
                    cfg,
                )
                log.warning("watchdog: feed silent (age=%s) — page dispatched", age)
            elif action == "recover":
                push_text("Tradewell feed recovered",
                          "Ticks are flowing again — engine sees the market.", cfg)
                log.info("watchdog: feed recovered — all-clear dispatched")
        except Exception:  # pragma: no cover - the watchdog must outlive its own bugs
            log.debug("watchdog check failed", exc_info=True)
