"""Trade monitoring: live P&L, trailing-stop ratchet, and exit recommendation.

Pure logic that mutates a Trade in place given the current premium, underlying
spot, and IST time-of-day. Recommendations are advisory only — the monitor never
closes a trade; the user acts manually and clicks Exit / Book Partial.
"""
from __future__ import annotations

import time

from app.trades.models import Trade, TradeAction, TradeEvent, TradeStatus

_TRAIL_PCT = 0.12                   # once in profit, trail 12% below the live premium

# R&D touch-ladder levels (premium % from entry, applied both signs). FROZEN,
# not a config knob: changing levels mid-stream resets cross-trade
# comparability, exactly the setup-detector parameter rule. The analysis
# windows (30/120/240 min) live in the R&D layer; recording is unconditional.
TOUCH_LEVELS_PCT = (3.0, 5.0, 10.0, 20.0)
# Tolerance far below tick resolution: an exact tick-aligned level print
# (entry 100.0 -> 105.0 is EXACTLY +5.0) must not miss its stamp to float
# representation error (review C3).
_TOUCH_EPS = 1e-9
_INTRADAY_EXIT_MIN = 15 * 60 + 10   # 15:10 IST — start flagging intraday exit

AUTO_CLOSE_NAMES = ("stop", "target1", "target2", "invalidation", "time_exit", "stall",
                    "quick_bank")


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
    if "stall" in enabled and trade.recommendation is TradeAction.STALL:
        return "stall"
    # BANK-THE-QUICK-TARGET (single lot only; enabled via QUICK_BANK_SINGLE_LOT
    # for paper, or by adding "quick_bank" to AUTO_CLOSE_TRIGGERS for the live
    # journal). One lot cannot book half at the quick target, so the default
    # ratchet leaves the first +12% unbanked; this policy takes the whole
    # position there instead. Checked BEFORE the targets: the policy's whole
    # point is that the trade ends at the first quick-target touch, so a bar
    # that reaches T1 too still records the quick-bank exit, not the runner it
    # would have declined to hold. Losses (stop/invalidation) still outrank it.
    # Strict initial_quantity: None means single-lot-at-entry is UNPROVABLE
    # (legacy row) — after a partial, quantity==lot_size is true of a de-risked
    # multi-lot position too, so falling back to quantity would bank the wrong
    # trades. No proof, no policy.
    if (
        "quick_bank" in enabled
        and trade.mode.value in ("intraday", "scalp")
        and trade.quick_target
        and px >= trade.quick_target
        and trade.initial_quantity == trade.lot_size
    ):
        return "quick_bank"
    if "target2" in enabled and px >= trade.target2:
        return "target2"
    if "target1" in enabled and px >= trade.target1:
        return "target1"
    return None


def _event(trade: Trade, kind: str, note: str) -> None:
    trade.events.append(TradeEvent(ts=int(time.time()), kind=kind, note=note))


def _ist_date_of(ts: int) -> str:
    t = time.gmtime(ts + 19800)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


# Advisory transitions worth a journal entry — the moment the monitor FIRST
# tells the user to act is exactly what a review of the trade needs later.
_EVENTFUL = {TradeAction.STOPLOSS, TradeAction.INVALIDATED, TradeAction.TARGET2,
             TradeAction.TIME_EXIT, TradeAction.STALL}


def _set_reco(trade: Trade, rec: TradeAction, note: str) -> None:
    if rec is not trade.recommendation and rec in _EVENTFUL:
        _event(trade, rec.value, note)
    trade.recommendation = rec
    trade.recommendation_note = note


def invalidation_unacked(trade: Trade) -> bool:
    """A break was latched and no acknowledgment is STRICTLY newer than it.

    A tie (re-break in the same second as an ack) counts as unacknowledged —
    when in doubt, nag.
    """
    return (
        trade.invalidation_fired_at is not None
        and (trade.invalidation_ack_at is None
             or trade.invalidation_ack_at <= trade.invalidation_fired_at)
    )


def track_reversible(trade: Trade, current_premium: float | None) -> None:
    """Observation-only tracking for an auto-closed (reversible) row.

    Deliberately writes post_close_* fields, NOT mfe/mae: the excursion
    evidence base assumes trade-life bounds, and contaminating it with
    post-exit extremes corrupts the target curve. If the close turns out to be
    FALSE, `reopen` folds these back into the real excursions — the trade was
    open the whole time; if it was real, the observation stays clearly
    separated from the trade's own record.
    """
    if current_premium is None or current_premium <= 0:
        return
    now_ts = int(time.time())
    trade.current_premium = current_premium
    if trade.post_close_mfe is None or current_premium > trade.post_close_mfe:
        trade.post_close_mfe, trade.post_close_mfe_at = current_premium, now_ts
    if trade.post_close_mae is None or current_premium < trade.post_close_mae:
        trade.post_close_mae, trade.post_close_mae_at = current_premium, now_ts
    # Ladder observations continue through the reversible window into their
    # own dict (same separation as post_close_mfe/mae): if the close proves
    # FALSE, reopen() folds them into touch_times with these REAL timestamps;
    # if it was real, they stay off the trade's record (review C2/C4).
    entry = trade.entry_premium
    if entry:
        move_pct = (current_premium - entry) / entry * 100.0
        for lv in TOUCH_LEVELS_PCT:
            if move_pct >= lv - _TOUCH_EPS:
                trade.post_close_touch_times.setdefault(f"+{lv:g}", now_ts)
            if move_pct <= -(lv - _TOUCH_EPS):
                trade.post_close_touch_times.setdefault(f"-{lv:g}", now_ts)


def latch_invalidation(trade: Trade, invalidated: bool, now_ts: int) -> None:
    """Edge-triggered break latch — the fix for the ack-nullifying loop.

    A latch fires only for a NEW episode: the first break ever, or a break
    after spot was OBSERVED back inside since the acknowledgment. While a
    breach persists, an ack covers all of it — level-triggered re-stamping
    made each ack worth one monitor cycle.
    """
    if not invalidated:
        trade.invalidation_clear_at = now_ts
        return
    if invalidation_unacked(trade):
        return                                   # episode already latched
    first_break = trade.invalidation_fired_at is None
    # `>=`: at 1-second stamps, a recovery observed in the ack's own second
    # counts as after it — a tie re-arms the latch (when in doubt, nag).
    new_episode = (
        trade.invalidation_ack_at is not None
        and trade.invalidation_clear_at is not None
        and trade.invalidation_clear_at >= trade.invalidation_ack_at
    )
    if first_break or new_episode:
        trade.invalidation_fired_at = now_ts


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
    stall_minutes: int | None = None,
    early_derisk_pct: float | None = None,
) -> None:
    if trade.status not in (TradeStatus.ENTERED, TradeStatus.PARTIAL):
        return

    intraday_close = (
        trade.mode.value in ("intraday", "scalp")
        and ist_minutes is not None
        and ist_minutes >= _INTRADAY_EXIT_MIN
    )

    if current_premium is None or current_premium <= 0:
        # No live premium (unsubscribed strike after a gap, restart, off-window).
        # Spot-based checks still work — a blind monitor must not miss an
        # invalidation, and must not clobber a persisted advisory with HOLD.
        # The latch also needs the CLEAR observation (spot back inside) here,
        # or a blind stretch could hide the recovery that arms the next episode.
        if spot is not None:
            latch_invalidation(trade, _invalidated(trade, spot), int(time.time()))
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

    # NEXT-SESSION FIRST PRINT, latched once. A position that survived an IST
    # day boundary records the first premium the new session actually trades —
    # entry -> this print IS the overnight gap, the one component of a hold no
    # stop can manage (28->29 Jul: +230 index points before any order could
    # act). Sampled at monitor cadence, so it is the first OBSERVED print, a
    # few seconds after the true open at worst.
    if (
        ist_date is not None
        and trade.next_open_premium is None
        and ist_date > _ist_date_of(trade.entered_at)
    ):
        trade.next_open_premium = current_premium
        trade.next_open_at = now_ts
        gap = (current_premium - entry) / entry * 100 if entry else 0.0
        _event(trade, "next_open",
               f"First print of the new session ₹{current_premium} "
               f"({gap:+.1f}% vs entry ₹{entry}) — the overnight gap, on the record")
    trade.pnl = round((current_premium - entry) * trade.quantity + trade.realized_pnl, 2)
    trade.pnl_pct = round((current_premium - entry) / entry * 100, 1) if entry else None

    # --- R&D touch ladder (docs/rnd-tab-plan-2026-08-12.md, R1) ---
    # First observed crossing of each fixed level, both signs, latched once.
    # setdefault IS the latch. A gap straight to +12% stamps +3/+5/+10 at the
    # same observation — correct: that IS when each level was first seen.
    # Sampled at monitor cadence (~5s), same precision class as mfe/mae.
    # touch_from marks when observation BEGAN: the analysis layer treats a
    # ladder as exact only when observation started at entry — a trade that
    # predates the ladder deploy gets stamps that are not first crossings,
    # and must be classed approx, not exact (review C1).
    if entry:
        move_pct = (current_premium - entry) / entry * 100.0
        if trade.touch_from is None:
            trade.touch_from = now_ts
        for lv in TOUCH_LEVELS_PCT:
            if move_pct >= lv - _TOUCH_EPS:
                trade.touch_times.setdefault(f"+{lv:g}", now_ts)
            if move_pct <= -(lv - _TOUCH_EPS):
                trade.touch_times.setdefault(f"-{lv:g}", now_ts)

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
        # The premium THIS cycle — what a bank-at-the-quick-target exit would
        # actually fill at (a gap past the target fills at the observed price,
        # not the level). The exit_ab counterfactual reads it; without it the
        # banking arm is systematically understated on every gap-through.
        trade.t0_cross_premium = current_premium
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
    elif trade.t0_hit:
        # DEAD-ZONE RATCHET (quick target hit, Target 1 not yet): the old
        # behaviour parked the stop at entry and waited for a T1 that almost
        # never comes intraday — both 22-Jul paper winners gave back 40-50% of
        # their peak between here and T1. Lock 55% of the run BEYOND the quick
        # target on each new high; anchored so the stop equals entry exactly at
        # the T0 cross (existing tests and semantics unchanged at that moment).
        qt = trade.quick_target or entry
        high = trade.mfe_premium or current_premium
        locked = round(entry + 0.55 * max(0.0, high - qt), 2)
        trade.trailing_sl = max(trade.trailing_sl, trade.stop_loss, locked)
    else:
        trade.trailing_sl = trade.stop_loss
        # POSITIONAL BREAKEVEN: a multi-day thesis that has already shown +5%
        # MFE must not be allowed to round-trip to its full stop (the open
        # 23-Jul positional rode 172 -> 181.65 -> 131.65 with no de-risking).
        if (
            trade.mode.value == "positional"
            and trade.mfe_premium is not None
            and trade.mfe_premium >= entry * 1.05
            and trade.stop_loss < entry
        ):
            trade.stop_loss = entry
            trade.trailing_sl = max(trade.trailing_sl, entry)
            _event(trade, "breakeven",
                   f"MFE ₹{trade.mfe_premium} ≥ +5% — SL moved to entry ₹{entry} (positional de-risk)")
        # EARLY DE-RISK (intraday/scalp, 28-Jul): between entry and the +12%
        # quick target there was NO de-risking at all — the audited week had
        # TEN fills peak between +1.9% and +11.3% and close negative, every
        # single one (+60pp of paper profit became -118pp of realised loss),
        # while the one fill that crossed +12% was protected to -0.5%. Once
        # MFE clears the configured fraction (default +5%, same trigger the
        # positional rule uses), the stop moves to entry. Setting stop_loss
        # (not just trailing_sl) also retires the disaster backstop, exactly
        # like the positional rule and the t0 latch.
        if (
            early_derisk_pct
            and trade.mode.value in ("intraday", "scalp")
            and trade.mfe_premium is not None
            and trade.mfe_premium >= entry * (1 + early_derisk_pct)
            and trade.stop_loss < entry
        ):
            trade.stop_loss = entry
            trade.trailing_sl = max(trade.trailing_sl, entry)
            _event(trade, "early_derisk",
                   f"MFE ₹{trade.mfe_premium} ≥ +{early_derisk_pct:.0%} — SL to entry "
                   f"₹{entry} (early de-risk; the +12% trigger was too far to protect this)")

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
    # The disaster backstop governs ONLY while the stop still sits below entry.
    # Once ANY de-risk lifted the stop to entry or above (T0, T1, or the
    # positional breakeven), the raised stop is the operative exit — the first
    # breakeven implementation changed only the displayed number while this
    # selection kept routing exits to the disaster level ₹40 lower (review
    # catch: protection that exists only in the journal text is not protection).
    if (
        trade.disaster_sl and not trade.t1_hit and not trade.t0_hit
        and trade.stop_loss < entry
    ):
        exit_level, exit_label = trade.disaster_sl, "Disaster stop"
    else:
        exit_level, exit_label = trade.trailing_sl, "Stop-loss"
    effective_sl = trade.trailing_sl        # what the UI and Kite hand-off show

    # Edge-triggered break latch: a new episode fires only after spot was seen
    # back inside; an ack silences the whole continuous breach it covers.
    latch_invalidation(trade, invalidated, now_ts)

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
                     f"Early target ₹{trade.quick_target} reached — book half, SL ratcheted to ₹{effective_sl}")
    elif trade.t0_hit:
        rec, note = (TradeAction.TRAIL_SL,
                     f"Early target reached — risk-free, SL ratcheted to ₹{effective_sl}")
    elif trade.t1_hit:
        rec, note = TradeAction.TRAIL_SL, f"In profit — trailing SL at ₹{effective_sl}"
    elif (
        # THESIS-STALL TIME STOP — the missing fourth exit type. A bought
        # option that hasn't reached even the quick target after its allotted
        # minutes is paying theta for a thesis that is merely late; recorded
        # excursions show these bleed out far more often than they recover.
        # Intraday only: a positional thesis is entitled to take days.
        # With no quick target on the row there is no objective bar, so the
        # fallback demands the position be essentially flat or worse (≤ +2%):
        # a profitable trade must never be stalled out on the clock alone.
        stall_minutes
        and trade.mode.value in ("intraday", "scalp")
        and (now_ts - trade.entered_at) >= stall_minutes * 60
        and (trade.quick_target is not None or (trade.pnl_pct or 0.0) <= 2.0)
    ):
        mins = (now_ts - trade.entered_at) // 60
        why = (f"quick target ₹{trade.quick_target} never reached"
               if trade.quick_target is not None
               else f"still {trade.pnl_pct:+.1f}% with no early-target level set")
        rec, note = (TradeAction.STALL,
                     f"No follow-through in {mins}m ({why}) — theta is winning, consider exiting")
    else:
        rec, note = TradeAction.HOLD, f"Hold — SL ₹{effective_sl}, T1 ₹{trade.target1}"

    # STICKY INVALIDATION. A break that fired stays the recommendation until
    # explicitly acknowledged — spot wandering back inside must not quietly
    # restore "Hold" (23-Jul: two ignored alerts, advice self-reverted, the
    # hold survived on luck). Real exits (stop/T2/close/stall) still outrank.
    if (
        invalidation_unacked(trade)
        and rec in (TradeAction.HOLD, TradeAction.TRAIL_SL, TradeAction.BOOK_PARTIAL)
    ):
        mins_ago = max(0, (now_ts - (trade.invalidation_fired_at or now_ts)) // 60)
        inside = "" if invalidated else " (spot back inside, but the thesis DID break)"
        rec = TradeAction.INVALIDATED
        note = (f"Invalidation fired {mins_ago}m ago and is UNACKNOWLEDGED{inside} — "
                "exit, or acknowledge to keep holding by choice")

    # GIVE-BACK WARNING: more than half of a meaningful peak profit has been
    # returned. Not an exit by itself — but a hold that has quietly become a
    # round trip must say so out loud.
    if (
        rec in (TradeAction.HOLD, TradeAction.TRAIL_SL)
        and trade.mfe_premium is not None
        and trade.mfe_premium >= entry * 1.05
        and (current_premium - entry) < 0.5 * (trade.mfe_premium - entry)
    ):
        if current_premium < entry:
            note += f" · ⚠ ENTIRE peak profit given back (peak ₹{trade.mfe_premium}, now below entry)"
        else:
            gave = min(100.0, (1 - (current_premium - entry) / (trade.mfe_premium - entry)) * 100)
            note += f" · ⚠ {gave:.0f}% of peak profit given back (peak ₹{trade.mfe_premium})"

    # Expiry-day theta warning for passive advice — on the contract's last day
    # time decay accelerates sharply; don't let "Hold" read as safe.
    if (
        ist_date is not None and trade.expiry == ist_date
        and rec in (TradeAction.HOLD, TradeAction.TRAIL_SL)
    ):
        note += " · EXPIRY DAY — theta decays fast, prefer exiting early"

    _set_reco(trade, rec, note)
