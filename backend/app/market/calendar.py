"""NSE trading-session calendar — the single source of truth for session logic.

Round-2 audit root cause: session rules were scattered across four places
(ws.is_market_open, services._news_hours, candle session constants, and no gate
at all on signal evaluation) with inconsistent behavior on holidays. Everything
session-related should route through here.

Holiday list: NSE equity/F&O weekday trading holidays. Cross-checked against
Zerodha's holiday calendar and ClearTax (2026-07-20). Weekend-falling holidays
are omitted (markets are closed anyway). Verify against the NSE circular when a
new year's list is published, and extend HOLIDAYS for the new year.

Fail-open by design: a date missing from the list behaves like a trading day —
the tool degrades to "open but stale" rather than silently disabling signals.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# Session bounds, minutes since IST midnight.
OPEN_MIN = 9 * 60 + 15          # 09:15
CLOSE_MIN = 15 * 60 + 30        # 15:30

HOLIDAYS: set[str] = {
    # --- 2026 (source: Zerodha holiday calendar / NSE circular) ---
    "2026-01-15",  # Maharashtra municipal elections
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-26",  # Shri Ram Navami
    "2026-03-31",  # Shri Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Bakri Id
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali – Balipratipada
    "2026-11-24",  # Prakash Gurpurb Sri Guru Nanak Dev
    "2026-12-25",  # Christmas
}


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(d: date | None = None) -> bool:
    d = d or now_ist().date()
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def is_market_open(dt: datetime | None = None) -> bool:
    """Inside the 09:15–15:30 IST session of a trading day."""
    dt = dt or now_ist()
    if not is_trading_day(dt.date()):
        return False
    minutes = dt.hour * 60 + dt.minute
    return OPEN_MIN <= minutes <= CLOSE_MIN


def market_closed_reason(dt: datetime | None = None) -> str:
    """Human-readable reason when the market is closed (for no-trade cards)."""
    dt = dt or now_ist()
    d = dt.date()
    if d.weekday() >= 5:
        return "Market closed — weekend"
    if d.isoformat() in HOLIDAYS:
        return "Market closed — NSE trading holiday"
    minutes = dt.hour * 60 + dt.minute
    if minutes < OPEN_MIN:
        return "Market closed — opens 09:15 IST"
    return "Market closed — session over (15:30 IST)"


def in_news_hours(dt: datetime | None = None) -> bool:
    """News polling window: 07:00–17:00 IST on trading days (pre-market news
    matters; overnight/holiday headlines can't affect a signal within the 6h
    sentiment window)."""
    dt = dt or now_ist()
    return is_trading_day(dt.date()) and 7 <= dt.hour < 17
