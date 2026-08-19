"""Trading-day and weekly-expiry calendar for the Closing Day study.

TRADING DAYS are not assumed — they are read off the candle spine. A day the
index printed 5-min bars is a day the market was open, which handles every NSE
holiday, muhurat session and unscheduled closure exactly, with no hardcoded
holiday list to rot.

WEEKLY EXPIRY is the one thing that cannot be read off index data, because
expired option contracts are unfetchable (Kite returns "invalid token" the day
after expiry — that is the whole reason this module models premiums instead of
reading them). So the expiry weekday is a stated ASSUMPTION:

    NIFTY weekly expiry was Thursday, and moved to Tuesday with effect from
    2025-09-02.

The live instrument dump agrees with the Tuesday half (2026-08-18, 08-25,
09-01, 09-08 are all Tuesdays) but says nothing about 2025 — the pre-switch
leg rests on the documented NSE change and nothing in this repo can verify it.
Both the weekday and the switch date are config, and study.py buckets every
result by days-to-expiry so a wrong calendar shows up as a shifted bucket
rather than a silently wrong headline.

If the nominal expiry weekday is a holiday, NSE settles on the previous
trading day; that rollback uses the real trading-day set, so it is exact.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional

IST = timezone(timedelta(hours=5, minutes=30))

# Monday=0 .. Sunday=6
THURSDAY = 3
TUESDAY = 1

SETTLEMENT_TIME = time(15, 30)  # NIFTY options settle on the expiry-day close


def ist_dt(ts: int | float) -> datetime:
    """Epoch seconds -> IST wall-clock datetime (tz-aware)."""
    return datetime.fromtimestamp(float(ts), IST)


def ist_date(ts: int | float) -> date:
    return ist_dt(ts).date()


def ist_minutes(ts: int | float) -> int:
    """IST minute-of-day for an epoch timestamp — the greppable home for the
    +19800 arithmetic that was previously inlined at two call sites."""
    return (int(ts + 19800) % 86400) // 60


def settlement_ts(expiry: date) -> float:
    """Epoch seconds of the 15:30 IST settlement on `expiry`."""
    return datetime.combine(expiry, SETTLEMENT_TIME, tzinfo=IST).timestamp()


class ExpiryCalendar:
    """Resolves the weekly expiry that a trade opened on day D can be held in.

    `trading_days` must be the sorted set of real session dates.
    """

    def __init__(self, trading_days: Iterable[date], switch_date: date,
                 weekday_before: int = THURSDAY, weekday_after: int = TUESDAY) -> None:
        self.trading_days = sorted(set(trading_days))
        self._day_set = set(self.trading_days)
        self.switch_date = switch_date
        self.weekday_before = weekday_before
        self.weekday_after = weekday_after

    def weekday_for(self, d: date) -> int:
        return self.weekday_after if d >= self.switch_date else self.weekday_before

    def _nominal_expiry_on_or_after(self, d: date) -> date:
        """The first date >= d falling on that week's nominal expiry weekday."""
        wd = self.weekday_for(d)
        delta = (wd - d.weekday()) % 7
        return d + timedelta(days=delta)

    def _settle_day(self, nominal: date) -> Optional[date]:
        """Nominal expiry rolled back to the previous trading day if it is a
        holiday.

        The roll-back only runs INSIDE the calendar's coverage. Past the last
        known session there is no holiday information, so the nominal weekday
        is returned untouched — a roll-back there would walk backwards through
        dates the data simply does not contain and land on the final stored
        session, dating the expiry days early. That bug shortened the most
        recent trade's DTE from 7 to 1 and roughly doubled its modelled
        return before it was caught.
        """
        if not self._day_set:
            return None
        if nominal > self.trading_days[-1]:
            return nominal
        probe = nominal
        for _ in range(7):
            if probe in self._day_set:
                return probe
            probe -= timedelta(days=1)
        return None

    def holdable_expiry(self, trade_day: date) -> Optional[date]:
        """The nearest weekly expiry a position opened on `trade_day` at 15:00
        can still be holding the next morning.

        Strictly AFTER trade_day: an option expiring on the trade day settles
        at 15:30 that afternoon, so there is nothing left to sell at 09:50.
        On expiry day the trade therefore rolls to the following week — which
        is what a trader would actually do, and it is why the DTE distribution
        in the results has a 7-day spike.

        The walk forward is a LOOP, not a single fallback: a holiday can pull
        this week's settlement back onto (or before) the trade day, and then
        the next nominal weekday is still the same already-settled week. One
        fallback step returned None for exactly that case.
        """
        nominal = self._nominal_expiry_on_or_after(trade_day)
        for _ in range(4):   # a month of look-ahead is more than enough
            settle = self._settle_day(nominal)
            if settle is not None and settle > trade_day:
                return settle
            nominal = self._nominal_expiry_on_or_after(nominal + timedelta(days=1))
        return None

    def next_trading_day(self, d: date) -> Optional[date]:
        for probe in self.trading_days:
            if probe > d:
                return probe
        return None
