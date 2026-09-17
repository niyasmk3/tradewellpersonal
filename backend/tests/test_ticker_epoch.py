"""Tick timestamps must survive a non-IST host clock (17-Sep).

kiteconnect hands the feed `datetime.fromtimestamp(exchange_epoch)` — naive,
in the HOST zone. Re-labelling that as IST is exact on an IST Mac and 90
minutes wrong on a UTC+4 one, which is where this Mac was on 17-Sep: tick
age 5400s all session, the supervisor looping, candles keyed early. This
pins the round trip under both clocks.

Run:  python backend/tests/test_ticker_epoch.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import time
from datetime import datetime, timedelta, timezone

from app.kite.ticker import _epoch

EXCHANGE_EPOCH = 1789633705          # 2026-09-17 13:58:25 IST, a real tick time
IST = timezone(timedelta(hours=5, minutes=30))


def _under_host_zone(tz: str, fn):
    saved = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def test_round_trip_is_exact_on_an_ist_host_and_on_a_gulf_host():
    for tz in ("Asia/Kolkata", "Asia/Dubai", "UTC", "America/New_York"):
        got = _under_host_zone(tz, lambda: _epoch(datetime.fromtimestamp(EXCHANGE_EPOCH)))
        assert got == EXCHANGE_EPOCH, (tz, got - EXCHANGE_EPOCH)
    print("  EPOCH  -> fromtimestamp() -> _epoch() exact under IST, Dubai, UTC, NY")


def test_the_old_relabel_was_ninety_minutes_wrong_in_dubai():
    """Documents the bug this replaces: naive host-local re-stamped as IST."""
    def old(dt):
        return int(dt.replace(tzinfo=IST).timestamp())
    wrong = _under_host_zone("Asia/Dubai", lambda: old(datetime.fromtimestamp(EXCHANGE_EPOCH)))
    assert EXCHANGE_EPOCH - wrong == 5400, EXCHANGE_EPOCH - wrong
    right = _under_host_zone("Asia/Kolkata", lambda: old(datetime.fromtimestamp(EXCHANGE_EPOCH)))
    assert right == EXCHANGE_EPOCH
    print("  EPOCH  -> the old relabel: exact in IST, 5400s early in Dubai (the bug)")


def test_aware_and_missing_inputs():
    aware = datetime.fromtimestamp(EXCHANGE_EPOCH, IST)
    assert _epoch(aware) == EXCHANGE_EPOCH
    assert _epoch(None) is None
    assert _epoch("not a datetime") is None
    print("  EPOCH  -> aware input exact; None / garbage -> None")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
