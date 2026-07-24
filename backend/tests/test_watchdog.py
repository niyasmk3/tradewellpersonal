"""Dead-feed watchdog state machine.

The watchdog exists because a 77-minute silent blackout once hid a full day's
move. These pin the properties that make it trustworthy: full grace before the
first page (even with stale prior-session ticks), silence measured only over
WATCHED session time, one page per 10 minutes no matter how episodes slice,
hysteresis on the all-clear, and quiet outside market hours.

Run:  python backend/tests/test_watchdog.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.watchdog import FeedWatchdog

T0 = 1_000_000.0
LIMIT = 150


def _warm(wd: FeedWatchdog, until: float = 300.0) -> float:
    """Healthy checks from T0 so the session baseline is older than the limit —
    the steady mid-session state every starvation scenario starts from."""
    t = T0
    while t < T0 + until:
        assert wd.check(t, 5.0, True, LIMIT) is None
        t += 30
    return t


def test_grace_covers_fresh_start_and_stale_prior_session():
    """age=None (no tick yet) and age=hours (yesterday's ticks) must both get
    the FULL configured window from session start before any page: silence is
    measured over watched time, never inherited from before the open."""
    for age in (None, 60_000.0):
        wd = FeedWatchdog()
        assert wd.check(T0, age, True, LIMIT) is None            # baseline set
        assert wd.check(T0 + 60, age, True, LIMIT) is None
        assert wd.check(T0 + 140, age, True, LIMIT) is None      # inside grace
        assert wd.check(T0 + 151, age, True, LIMIT) == "page"    # grace exhausted
    print("  WDOG   -> no tick / stale-yesterday tick: full 150s watched grace")


def test_healthy_feed_never_pages():
    wd = FeedWatchdog()
    _warm(wd, 1200.0)
    print("  WDOG   -> fresh ticks: 20 minutes of checks, zero pages")


def test_page_then_repage_every_10_minutes():
    wd = FeedWatchdog()
    t = _warm(wd)
    assert wd.check(t, 170.0, True, LIMIT) == "page"             # blackout begins
    first_page = t
    dt = 30.0
    while t + dt - first_page < 600:                             # inside cooldown
        assert wd.check(t + dt, 170.0 + dt, True, LIMIT) is None
        dt += 30
    assert wd.check(first_page + 601, 800.0, True, LIMIT) == "page"
    print("  WDOG   -> persistent blackout: one page, silence, re-page at +10min")


def test_cooldown_spans_episodes_no_page_storm():
    """A half-broken socket (a tick every ~3 minutes) flips healthy/starved on
    alternate checks. The 600s spacing must hold ACROSS episodes, and the
    flapping must not fire recovery pushes either."""
    wd = FeedWatchdog()
    t = _warm(wd)
    assert wd.check(t, 170.0, True, LIMIT) == "page"
    pushes = []
    for i in range(12):                                          # ~6 min flapping
        t += 30
        action = wd.check(t, 5.0 if i % 2 else 170.0, True, LIMIT)
        if action:
            pushes.append(action)
    # The healthy checks never streak 3 in a row, so no all-clear; the starved
    # checks sit inside the cooldown, so no page. Total extra pushes: zero.
    assert pushes == [], f"page/recovery storm: {pushes}"
    print("  WDOG   -> flapping socket: zero extra pushes inside the cooldown")


def test_recovery_needs_streak_and_a_prior_page():
    wd = FeedWatchdog()
    t = _warm(wd)
    assert wd.check(t, 170.0, True, LIMIT) == "page"
    assert wd.check(t + 30, 5.0, True, LIMIT) is None            # healthy x1
    assert wd.check(t + 60, 5.0, True, LIMIT) is None            # healthy x2
    assert wd.check(t + 90, 5.0, True, LIMIT) == "recover"       # x3 -> all-clear
    # A later starvation inside the cooldown pages nothing — and its recovery
    # must therefore stay silent too: no all-clear for an alarm nobody heard.
    assert wd.check(t + 120, 200.0, True, LIMIT) is None         # ep2, cooldown holds
    for i in (150, 180, 210):
        assert wd.check(t + i, 5.0, True, LIMIT) is None
    print("  WDOG   -> all-clear needs 3 healthy checks AND a page someone heard")


def test_closed_market_resets_and_reopens_with_grace():
    wd = FeedWatchdog()
    t = _warm(wd)
    assert wd.check(t, 170.0, True, LIMIT) == "page"
    assert wd.check(t + 30, 200.0, False, LIMIT) is None         # market closed
    # Next open: yesterday's tick age is enormous, but the baseline restarts
    # the watched-grace window from the open.
    t_open = t + 60_000
    assert wd.check(t_open, 60_000.0, True, LIMIT) is None
    assert wd.check(t_open + 100, 60_100.0, True, LIMIT) is None
    print("  WDOG   -> close resets state; reopen starts a fresh grace window")


def test_disabled_never_acts():
    wd = FeedWatchdog()
    for i in range(10):
        assert wd.check(T0 + i * 30, None, True, 0) is None
    print("  WDOG   -> limit 0: permanently silent")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
