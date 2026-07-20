"""Trade monitoring: live P&L, trailing-stop ratchet, and exit recommendation.

Pure logic that mutates a Trade in place given the current premium, underlying
spot, and IST time-of-day. Recommendations are advisory only — the monitor never
closes a trade; the user acts manually and clicks Exit / Book Partial.
"""
from __future__ import annotations

import time

from app.trades.models import Trade, TradeAction, TradeEvent, TradeStatus

_TRAIL_PCT = 0.12          # once in profit, trail 12% below the live premium
_INTRADAY_EXIT_MIN = 15 * 60 + 10   # 15:10 IST — start flagging intraday exit


def _event(trade: Trade, kind: str, note: str) -> None:
    trade.events.append(TradeEvent(ts=int(time.time()), kind=kind, note=note))


# Advisory transitions worth a journal entry — the moment the monitor FIRST
# tells the user to act is exactly what a review of the trade needs later.
_EVENTFUL = {TradeAction.STOPLOSS, TradeAction.INVALIDATED, TradeAction.TARGET2, TradeAction.TIME_EXIT}


def _set_reco(trade: Trade, rec: TradeAction, note: str) -> None:
    if rec is not trade.recommendation and rec in _EVENTFUL:
        _event(trade, rec.value, note)
    trade.recommendation = rec
    trade.recommendation_note = note


def _invalidated(trade: Trade, spot: float | None) -> bool:
    if trade.invalidation_level is None or spot is None:
        return False
    if trade.invalidation_dir == "above" and spot < trade.invalidation_level:
        return True
    if trade.invalidation_dir == "below" and spot > trade.invalidation_level:
        return True
    return False


def evaluate(
    trade: Trade,
    current_premium: float | None,
    spot: float | None,
    ist_minutes: int | None,
    ist_date: str | None = None,
) -> None:
    if trade.status not in (TradeStatus.ENTERED, TradeStatus.PARTIAL):
        return

    intraday_close = (
        trade.mode.value == "intraday"
        and ist_minutes is not None
        and ist_minutes >= _INTRADAY_EXIT_MIN
    )

    if current_premium is None or current_premium <= 0:
        # No live premium (unsubscribed strike after a gap, restart, off-window).
        # Spot-based checks still work — a blind monitor must not miss an
        # invalidation, and must not clobber a persisted advisory with HOLD.
        if _invalidated(trade, spot):
            _set_reco(trade, TradeAction.INVALIDATED,
                      f"{trade.symbol} broke invalidation {trade.invalidation_level:.0f} — exit (premium stale)")
        elif intraday_close:
            _set_reco(trade, TradeAction.TIME_EXIT,
                      "Approaching market close — exit intraday position (premium stale)")
        elif trade.recommendation == TradeAction.HOLD:
            trade.recommendation_note = "No live premium yet"
        # otherwise: keep the existing recommendation untouched
        return

    trade.current_premium = current_premium
    entry = trade.entry_premium
    trade.pnl = round((current_premium - entry) * trade.quantity + trade.realized_pnl, 2)
    trade.pnl_pct = round((current_premium - entry) / entry * 100, 1) if entry else None

    # --- trailing-stop ratchet ---
    if not trade.t1_hit and current_premium >= trade.target1:
        trade.t1_hit = True
        if trade.stop_loss < entry:
            trade.stop_loss = entry
        _event(trade, "target1", f"Target 1 ₹{trade.target1} reached — SL moved to entry ₹{entry}")

    if trade.t1_hit:
        trailed = round(current_premium * (1 - _TRAIL_PCT), 2)
        trade.trailing_sl = max(trade.trailing_sl, trade.stop_loss, trailed)
    else:
        trade.trailing_sl = trade.stop_loss

    effective_sl = trade.trailing_sl
    invalidated = _invalidated(trade, spot)

    # --- recommendation (priority order) ---
    if current_premium <= effective_sl:
        rec, note = TradeAction.STOPLOSS, f"Stop-loss ₹{effective_sl} hit — exit now"
    elif invalidated:
        rec, note = (
            TradeAction.INVALIDATED,
            f"{trade.symbol} broke invalidation {trade.invalidation_level:.0f} — exit",
        )
    elif current_premium >= trade.target2:
        rec, note = TradeAction.TARGET2, f"Target 2 ₹{trade.target2} reached — book remaining"
    elif intraday_close:
        # After 15:10 IST the answer for an intraday position is exit — even if
        # it's sitting above T1 (booking a partial and holding is not an option).
        rec, note = TradeAction.TIME_EXIT, "Approaching market close — exit intraday position"
    elif current_premium >= trade.target1:
        if trade.status == TradeStatus.ENTERED and trade.lots > 1:
            rec, note = TradeAction.BOOK_PARTIAL, f"Target 1 ₹{trade.target1} reached — book partial, trail the rest"
        else:
            # Single lot can't be partially booked — trail it instead.
            rec, note = TradeAction.TRAIL_SL, f"Target 1 reached — trailing SL at ₹{effective_sl}"
    elif trade.t1_hit:
        rec, note = TradeAction.TRAIL_SL, f"In profit — trailing SL at ₹{effective_sl}"
    else:
        rec, note = TradeAction.HOLD, f"Hold — SL ₹{effective_sl}, T1 ₹{trade.target1}"

    # Expiry-day theta warning for passive advice — on the contract's last day
    # time decay accelerates sharply; don't let "Hold" read as safe.
    if (
        ist_date is not None and trade.expiry == ist_date
        and rec in (TradeAction.HOLD, TradeAction.TRAIL_SL)
    ):
        note += " · EXPIRY DAY — theta decays fast, prefer exiting early"

    _set_reco(trade, rec, note)
