"""Supervisor starvation rule (17-Sep): silence is measured from the bell.

At 09:15:17 IST the newest tick was 61,812s old — the overnight gap — and
the supervisor called that "no ticks while market open", restarted the feed
on its first post-open check and replaced the whole process two checks
later. These pin the grace: the open starts the clock, a dead socket is
still caught three minutes in, and nothing fires outside the session.

Run:  python backend/tests/test_feed_starvation.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime

from app.market.calendar import IST
from app.services import FeedController

DAY = (2026, 9, 16)                 # Wednesday, a session with a logged card
LIMIT = 180


def _at(h, m, s=0):
    return datetime(*DAY, h, m, s, tzinfo=IST)


def test_overnight_age_at_the_bell_is_not_session_silence():
    """The 17-Sep morning, replayed: three supervisor checks after the open,
    each with the overnight age. None may fire."""
    for hms, age in (((9, 15, 17), 61812), ((9, 16, 24), 61879), ((9, 17, 31), 61946)):
        assert FeedController._tick_starved(LIMIT, now=_at(*hms), age=age) is False, hms
    print("  STARVE -> 09:15:17 / 09:16:24 / 09:17:31 with overnight age: no restart")


def test_a_dead_socket_is_still_caught_three_minutes_after_the_bell():
    assert FeedController._tick_starved(LIMIT, now=_at(9, 18, 0), age=62000) is False
    assert FeedController._tick_starved(LIMIT, now=_at(9, 18, 1), age=62000) is True
    # No tick at all this process: same clock, same grace.
    assert FeedController._tick_starved(LIMIT, now=_at(9, 17, 59), age=None) is False
    assert FeedController._tick_starved(LIMIT, now=_at(9, 18, 1), age=None) is True
    print("  STARVE -> silent since the bell: starved at 09:18:01, not before")


def test_mid_session_uses_the_tick_age_as_before():
    assert FeedController._tick_starved(LIMIT, now=_at(14, 0), age=5) is False
    assert FeedController._tick_starved(LIMIT, now=_at(14, 0), age=181) is True
    assert FeedController._session_silence(now=_at(14, 0), age=181) == 181.0
    print("  STARVE -> mid-session: tick age rules, 181s > 180s starves")


def test_never_outside_the_session():
    for hm in ((8, 0), (9, 14), (15, 31), (23, 59)):
        assert FeedController._tick_starved(LIMIT, now=_at(*hm), age=999_999) is False
        assert FeedController._session_silence(now=_at(*hm), age=1) is None
    print("  STARVE -> closed market: never starved, whatever the age")



# --- escalation memory (feed_escalation.py) -------------------------------------

def test_self_restart_is_rate_limited_across_process_generations():
    from app import feed_escalation as esc
    T0, DAY_ = 1_000_000.0, "2026-09-17"
    st = esc.load(None)                                   # a fresh interpreter
    assert esc.verdict(st, T0, DAY_) == "restart"
    st = esc.record(st, T0, DAY_)                         # ...execs itself
    assert st == {"day": DAY_, "count": 1, "last_at": T0}
    # The NEXT generation, three minutes later, reads the file and waits.
    assert esc.verdict(st, T0 + 180, DAY_) == "cooldown"
    assert esc.verdict(st, T0 + esc.COOLDOWN_S - 1, DAY_) == "cooldown"
    assert esc.verdict(st, T0 + esc.COOLDOWN_S, DAY_) == "restart"
    print("  ESCAL  -> one self-restart per %.0fs, remembered across exec"
          % esc.COOLDOWN_S)


def test_three_self_restarts_a_day_then_a_human():
    from app import feed_escalation as esc
    T0, DAY_ = 1_000_000.0, "2026-09-17"
    st, t = {}, T0
    for n in range(esc.MAX_PER_DAY):
        assert esc.verdict(st, t, DAY_) == "restart", n
        st = esc.record(st, t, DAY_)
        t += esc.COOLDOWN_S + 1
    assert esc.verdict(st, t, DAY_) == "exhausted"
    assert esc.verdict(st, t + 86400, DAY_) == "exhausted"     # cooldown is not the cure
    assert esc.verdict(st, t + 86400, "2026-09-18") == "restart"   # a new day is
    assert esc.record(st, t + 86400, "2026-09-18")["count"] == 1
    print("  ESCAL  -> %d/day, then exhausted until the next IST day" % esc.MAX_PER_DAY)


def test_escalation_state_round_trips_and_tolerates_a_bad_file():
    import tempfile
    from pathlib import Path
    from app import feed_escalation as esc
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".supervisor_state.json"
        assert esc.load(p) == {}
        esc.save({"day": "2026-09-17", "count": 2, "last_at": 5.0}, p)
        assert esc.load(p)["count"] == 2
        p.write_text("[not, an, object]")
        assert esc.load(p) == {}
    print("  ESCAL  -> state persists; a corrupt file reads as empty")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
