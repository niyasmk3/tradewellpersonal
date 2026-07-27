"""NSE calendar + expiry-rollover tests (round-2 audit fixes).

Run:  python backend/tests/test_calendar.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import date, datetime

from app.market import calendar as mcal
from app.kite.instruments import _nearest_monthly_expiry


def test_trading_days():
    assert mcal.is_trading_day(date(2026, 7, 20))          # Monday
    assert not mcal.is_trading_day(date(2026, 7, 18))      # Saturday
    assert not mcal.is_trading_day(date(2026, 7, 19))      # Sunday
    assert not mcal.is_trading_day(date(2026, 10, 20))     # Dussehra (weekday holiday)
    assert not mcal.is_trading_day(date(2026, 11, 24))     # Guru Nanak Jayanti
    # Fail-open: an unknown future date on a weekday counts as trading
    assert mcal.is_trading_day(date(2027, 7, 1))


def test_market_open_bounds():
    mk = lambda h, m: datetime(2026, 7, 20, h, m, tzinfo=mcal.IST)  # a Monday
    assert not mcal.is_market_open(mk(9, 14))
    assert mcal.is_market_open(mk(9, 15))
    assert mcal.is_market_open(mk(15, 30))
    assert not mcal.is_market_open(mk(15, 31))
    # Holiday: closed all day even inside session hours
    assert not mcal.is_market_open(datetime(2026, 10, 20, 10, 0, tzinfo=mcal.IST))


def test_closed_reasons():
    assert "weekend" in mcal.market_closed_reason(datetime(2026, 7, 19, 11, 0, tzinfo=mcal.IST))
    assert "holiday" in mcal.market_closed_reason(datetime(2026, 10, 20, 11, 0, tzinfo=mcal.IST))
    assert "opens" in mcal.market_closed_reason(datetime(2026, 7, 20, 8, 0, tzinfo=mcal.IST))
    assert "session over" in mcal.market_closed_reason(datetime(2026, 7, 20, 16, 0, tzinfo=mcal.IST))


def test_news_hours():
    assert mcal.in_news_hours(datetime(2026, 7, 20, 7, 30, tzinfo=mcal.IST))
    assert not mcal.in_news_hours(datetime(2026, 7, 20, 6, 0, tzinfo=mcal.IST))
    assert not mcal.in_news_hours(datetime(2026, 7, 20, 17, 30, tzinfo=mcal.IST))
    assert not mcal.in_news_hours(datetime(2026, 7, 19, 12, 0, tzinfo=mcal.IST))   # Sunday
    assert not mcal.in_news_hours(datetime(2026, 10, 20, 12, 0, tzinfo=mcal.IST))  # holiday


def _opts(*expiries):
    return [{"expiry": e} for e in expiries]


def test_monthly_rollover():
    # Monthlies: July 28, Aug 25; weeklies in between.
    options = _opts("2026-07-07", "2026-07-14", "2026-07-21", "2026-07-28",
                    "2026-08-04", "2026-08-25")
    # Normal mid-month day: July's monthly (the 28th) is picked.
    assert _nearest_monthly_expiry(options, date(2026, 7, 20)) == date(2026, 7, 28)
    # ON monthly expiry day: must roll to August — never build multi-day swing
    # cards on a contract dying this afternoon.
    assert _nearest_monthly_expiry(options, date(2026, 7, 28)) == date(2026, 8, 25)
    # Within the 2-day rollover window: also August.
    assert _nearest_monthly_expiry(options, date(2026, 7, 27)) == date(2026, 8, 25)
    # 3 days out: still July.
    assert _nearest_monthly_expiry(options, date(2026, 7, 24)) == date(2026, 7, 28)


def test_monthly_rollover_fallback():
    # Pathological: only the dying contract listed — fall back to it rather
    # than returning nothing.
    options = _opts("2026-07-28")
    assert _nearest_monthly_expiry(options, date(2026, 7, 28)) == date(2026, 7, 28)


def test_news_loop_dependencies_exist():
    """The 24-Jul regression: FeedController._news_hours was deleted while its
    _news_loop call survived, and every news cycle since crashed inside the
    loop's own error handler — a silent freeze, not a visible failure."""
    from app.services import FeedController

    assert callable(getattr(FeedController, "_news_hours", None)), \
        "_news_hours missing — the news loop will AttributeError every cycle"
    assert isinstance(FeedController._news_hours(), bool)
    print("  NEWSDEP-> _news_hours exists and answers; the loop cannot crash on it")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("\n" + ("ALL PASSED" if failed == 0 else f"{failed} FAILED"))
    sys.exit(1 if failed else 0)
