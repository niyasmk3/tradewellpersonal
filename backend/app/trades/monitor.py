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

AUTO_CLOSE_NAMES = ("stop", "target1", "target2", "invalidation", "time_exit")


def auto_close_trigger(trade: Trade, enabled: set[str]) -> str | None:
    """Which enabled trigger (if any) says this position should now be closed.

    Bookkeeping only. Tradewell places no order, so this records what the plan
    says happened — it cannot know that YOU actually exited, which is why the
    close it produces is marked `auto_closed` and stays reversible.

    Target 1 is tested on the PRICE, not on the recommendation: at T1 the
    monitor advises "book partial, trail the rest" (or trails a single lot), so
    it never emits a TARGET1 recommendation and a recommendation-based check
    would silently never fire.

    Order matters — stop and invalidation outrank the targets, so a bar that
    reaches both is recorded as the loss, never the win.
    """
    if trade.status not in (TradeStatus.ENTERED, TradeStatus.PARTIAL):
        return None
    px = trade.current_premium
    if px is None or px <= 0:
        return None                      # never close on a stale/absent premium

    if "stop" in enabled and trade.recommendation is TradeAction.STOPLOSS:
        return "stop"
    if "invalidation" in enabled and trade.recommendation is TradeAction.INVALIDATED:
        return "invalidation"
    if "time_exit" in enabled and trade.recommendation is TradeAction.TIME_EXIT:
        return "time_exit"
    if "target2" in enabled and px >= trade.target2:
        return "target2"
    if "target1" in enabled and px >= trade.target1:
        return "target1"
    return None


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

    # Excursion first, so it is recorded even on the cycle that closes the
    # trade — the extreme that triggers an exit is part of the trade's history.
    now_ts = int(time.time())
    if trade.excursion_from is None:
        trade.excursion_from = now_ts
    if trade.mfe_premium is None or current_premium > trade.mfe_premium:
        trade.mfe_premium, trade.mfe_at = current_premium, now_ts
    if trade.mae_premium is None or current_premium < trade.mae_premium:
        trade.mae_premium, trade.mae_at = current_premium, now_ts

    trade.current_premium = current_premium
    entry = trade.entry_premium
    trade.pnl = round((current_premium - entry) * trade.quantity + trade.realized_pnl, 2)
    trade.pnl_pct = round((current_premium - entry) / entry * 100, 1) if entry else None

    # --- early partial level ---
    # Reaching it latches t0_hit and lifts the stop to entry, so the remainder
    # runs risk-free. This is the answer to a trade that spikes and gives it all
    # back: on 21-Jul one ran +31% and still closed at a loss. Booking half here
    # banks that move WITHOUT capping the runner — which is the thing simply
    # lowering Target 1 would have cost.
    if (
        not trade.t0_hit
        and trade.quick_target
        and current_premium >= trade.quick_target
    ):
        trade.t0_hit = True
        if trade.stop_loss < entry:
            trade.stop_loss = entry
        _event(trade, "quick_target",
               f"Early target ₹{trade.quick_target} reached — SL to entry ₹{entry}"
               + (", book half" if trade.lots > 1 else " (single lot: now risk-free)"))

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
        # max(), not assignment: once T0 lifted stop_loss to entry the floor
        # must not slide back down on the next cycle.
        trade.trailing_sl = max(trade.trailing_sl, trade.stop_loss) if trade.t0_hit \
            else trade.stop_loss

    invalidated = _invalidated(trade, spot)

    # WHICH premium level actually ends the trade before Target 1.
    #
    # With `disaster_sl` set, the index invalidation is the primary stop and the
    # premium level is only a backstop. The reason is measured, not theoretical:
    # on 21-Jul-2026 a 0.14% index range produced an 83% swing in a 0DTE
    # premium, so an 18%-of-premium stop was worth ~10 index points inside a
    # 35-point noise band. It was touched 7 times while the index invalidation
    # was never breached — the thesis held all day and the stop measured gamma,
    # not risk.
    #
    # Once Target 1 is hit the trailing stop takes over regardless: by then the
    # position is in profit, the premium is larger so the same percentage is a
    # wider absolute band, and protecting the gain is the whole point.
    # Once half is banked and the stop is at entry, the wide backstop has done
    # its job — protecting the free runner is what matters now.
    if trade.disaster_sl and not trade.t1_hit and not trade.t0_hit:
        exit_level, exit_label = trade.disaster_sl, "Disaster stop"
    else:
        exit_level, exit_label = trade.trailing_sl, "Stop-loss"
    effective_sl = trade.trailing_sl        # what the UI and Kite hand-off show

    # --- recommendation (priority order) ---
    # Invalidation is tested FIRST: it is the structural thesis, and letting a
    # noisy premium level pre-empt it is what this whole change exists to stop.
    if invalidated:
        rec, note = (
            TradeAction.INVALIDATED,
            f"{trade.symbol} broke invalidation {trade.invalidation_level:.0f} — exit",
        )
    elif current_premium <= exit_level:
        rec, note = TradeAction.STOPLOSS, f"{exit_label} ₹{exit_level} hit — exit now"
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
    elif trade.t0_hit and trade.status == TradeStatus.ENTERED and trade.lots > 1:
        rec, note = (TradeAction.BOOK_PARTIAL,
                     f"Early target ₹{trade.quick_target} reached — book half, SL at entry ₹{entry}")
    elif trade.t0_hit:
        rec, note = (TradeAction.TRAIL_SL,
                     f"Early target reached — risk-free, SL at entry ₹{entry}")
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
