"""Paper trading: take every issued signal in simulation, exit it by the plan.

WHY: every claim in this project about the engine's edge rests on backtests,
which grade the underlying future and exclude theta, IV and premium spreads.
Paper trading forward on live ticks removes all of that — real signal timing,
real option premiums, no look-ahead, no survivorship. It is the only evidence
that answers "does this actually work" without risking a rupee.

HARD SEPARATION FROM REAL MONEY. Paper positions live in their own store file
and never touch .trades.json, so they cannot reach the realised P&L header, the
daily loss limit, or the circuit breakers that gate live signals. They are also
never reconciled against the broker's position book: a simulated position has
no counterpart at Zerodha, and matching one against the real book would close
it instantly (or worse, reopen a real row).

Fidelity choices, all of which push results DOWN rather than up:
  * fills cost slippage on both legs, because the tape's last price is not the
    ask you would actually pay;
  * P&L is net of the full Zerodha charge schedule;
  * the same open-position cap the live throttle applies is applied here, so
    the simulation does not take trades the live system would have blocked.
"""
from __future__ import annotations

import logging
import time

from app.config import Settings
from app.market.calendar import EVENING_MIN
from app.paper import charges as chg
from app.signals.models import SignalCard, TradingMode
from app.signals.modes import ladder_params
from app.signals.risk_limits import risk_limit_store
from app.state import MarketState
from app.trades import monitor
from app.trades.models import TradeStatus
from app.trades.store import TradeStore

log = logging.getLogger("tradewell.paper")

# Every plan trigger closes a paper position — unlike the live journal, where
# which triggers auto-close is a user setting. A simulation that needs a human
# to close it is not a simulation. "stall" is here BEFORE it auto-closes live
# rows on purpose: the paper book is where the stall rule earns (or loses) the
# right to touch real positions.
_EXIT_TRIGGERS = {"stop", "target1", "target2", "invalidation", "time_exit", "stall"}

# Fills became honest on 23-Jul-2026 (slippage + trigger-price cap). The book
# before that filled at prices the tape never printed — the "+₹9.5k day" the
# 22-Jul forensics restated to roughly −₹700. Those rows stay visible in the
# ledger, flagged, but every aggregate that gates a decision (scalp go-live,
# T1 calibration, expectancy) starts here.
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
HONEST_FILLS_FROM = int(_dt(2026, 7, 23, tzinfo=_tz(_td(hours=5, minutes=30))).timestamp())


def is_hollow_row(t) -> bool:
    """A counterfactual paper fill — any shadow class (floor, late, stopb).

    Tagged via the notes field at fill time. These rows are the COUNTERFACTUAL
    — the road not taken — so every consumer that grades the system's own
    decisions (summary aggregates, T1 calibration, exit analytics, excursion
    evidence, the refire guard) must exclude them; each class's own block in
    summarize() is where its hypothesis gets judged.
    """
    return bool(getattr(t, "notes", None) and t.notes.startswith("hollow:"))


def shadow_class(x) -> str | None:
    """Which counterfactual ledger a row/card belongs to: "late" (14:15-cutoff
    hypothesis), "refire" (re-fire-guard hypothesis, 03-Aug), "floor"
    (participation-floor hypothesis), "stopb" (the stop-basis A/B twin,
    audit P1-5), or None (clean).

    Works on a Trade (notes tag) or a SignalCard (hollow_reason). The classes
    must stay independent EVERYWHERE — including the paper capacity buckets:
    the review caught floor shadows starving late shadows out of fills when
    both shared one bucket, which biases both 30-fill verdicts. (stopb rows
    are exempt from capacity by construction: they only exist 1:1 with a
    clean fill that already cleared its own bucket.)
    """
    notes = getattr(x, "notes", None)
    if notes and notes.startswith("hollow:"):
        if notes.startswith("hollow: late:"):
            return "late"
        if notes.startswith("hollow: refire:"):
            return "refire"
        if notes.startswith("hollow: stopb:"):
            return "stopb"
        if notes.startswith("hollow: setup:"):
            return "setup"
        return "floor"
    reason = getattr(x, "hollow_reason", None)
    if reason:
        if reason.startswith("late:"):
            return "late"
        if reason.startswith("refire:"):
            return "refire"
        if reason.startswith("stopb:"):
            return "stopb"
        if reason.startswith("setup:"):
            return "setup"
        return "floor"
    return None


def _ist_minutes(now: int) -> int:
    return (now + 19800) % 86400 // 60


def _ist_date(now: int) -> str:
    t = time.gmtime(now + 19800)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


class PaperTradingService:
    def __init__(self, cfg: Settings, state: MarketState, store: TradeStore) -> None:
        self.cfg = cfg
        self.state = state
        self.store = store
        # Signal ids already acted on. Rebuilt from the store on startup so a
        # restart mid-session cannot double-enter the same card.
        self._seen: set[str] = {
            t.signal_id for t in store.all() if t.signal_id
        }
        # Cards offered but not (yet) filled: id -> (contract, last reason,
        # valid_until). A deferral is silent by design (it retries every
        # cycle); what must NOT be silent is the card EXPIRING un-filled —
        # each of those is a trade the evidence base quietly lost, and a
        # simulation that under-reports its own misses overstates its edge.
        self._deferred: dict[str, tuple[str, str, int]] = {}
        self.filled = 0
        self.expired_unfilled = 0

    # ---- entry -------------------------------------------------------------
    def scan(self, store) -> None:
        """Offer every currently-active signal to `consider`.

        Lives here rather than in the feed loop so it is covered by tests. The
        first version of this was inline in services.py, passed the configured
        modes through as raw STRINGS where a TradingMode was required, and threw
        `'str' object has no attribute 'value'` on every cycle for an entire
        session — silently, because the loop caught and logged it. Nothing was
        ever simulated, and no test touched the path.
        """
        for symbol in self.cfg.signal_symbols:
            for mode in self.cfg.signal_mode_list:
                try:
                    resp = store.latest(symbol, TradingMode(mode))
                except ValueError:               # unknown mode in config
                    continue
                if resp is not None and resp.signal is not None:
                    self.consider(resp.signal)

    def _defer(self, card: SignalCard, reason: str) -> None:
        self._deferred[card.id] = (card.contract, reason, card.valid_until)

    def sweep_expired(self, now: int | None = None) -> None:
        """Log, once, every offered card that expired without a fill."""
        now = now or int(time.time())
        for cid in [c for c, (_, _, vu) in self._deferred.items() if now >= vu]:
            contract, reason, _ = self._deferred.pop(cid)
            if cid in self._seen:        # filled later after an early deferral
                continue
            self.expired_unfilled += 1
            log.warning("paper: %s expired UNFILLED (last: %s) — signal %s never simulated",
                        contract, reason, cid)

    def consider(self, card: SignalCard) -> None:
        """Open a simulated position for a currently-valid signal."""
        if card.id in self._seen:
            return
        # A card the engine no longer considers tradeable must not be entered:
        # filling minutes after it expired would simulate a trade the live
        # system was not offering. `state`/`valid_until` are the engine's own
        # definition of freshness, so no second arbitrary staleness rule.
        if card.state.value != "active" or int(time.time()) >= card.valid_until:
            return                       # NOT marked seen — it may still be live next cycle

        # The premium is read BEFORE the card is marked seen, because the
        # entry-zone check below must be retryable: a premium currently outside
        # the zone can come back into it while the card is still valid, and
        # burning the card here would lose a trade the plan would have taken.
        #
        # TAPE ONLY — the card's reference is NOT a fallback. On 22-Jul the
        # 09:15:03 card carried yesterday's close as its reference; no tick had
        # arrived yet, the fallback "filled" at ₹198.24 while the contract
        # opened at ₹216, and the book recorded ₹8,261 of profit from an entry
        # that never traded. A simulation may only buy prices that existed.
        tick = self.state.ticks.get(card.token, {}) if card.token is not None else {}
        base = tick.get("last_price")
        if not base or base <= 0:
            self._defer(card, "no tick")
            return                       # no tick yet — retry next cycle
        ts = tick.get("ts")
        max_age = self.cfg.signal_max_premium_age_s
        if max_age > 0 and (not ts or int(time.time()) - int(ts) > max_age):
            # Same rule as the live engine: a tick with no exchange stamp is
            # UNVERIFIABLE, which for filling at its price is the same as
            # stale — the 22-Jul phantom fill was exactly a carried-over price
            # nothing had aged. Gate off (0) accepts unstamped ticks (tests,
            # offline replay).
            age = f"{int(time.time()) - int(ts)}s old" if ts else "unstamped"
            log.info("paper: %s tick is %s — waiting for a live quote", card.contract, age)
            self._defer(card, f"tick {age}")
            return                       # NOT marked seen — the stream may recover

        # ENTRY-ZONE FLOOR. Previously only the top was clamped, so the
        # simulator would happily buy a premium that had already collapsed
        # below the zone — on 21-Jul it filled at 23.59 against a published
        # zone of 26.85-27.65 (12% below), inherited the card's stop verbatim,
        # and was left with 1.39 of room. That is not the trade the plan
        # proposed. For a PE, a premium falling this far inside the validity
        # window means the index rallied — the setup was already going wrong,
        # and buying the dip into it is the opposite of the signal.
        if base < card.entry_low:
            log.info("paper: %s at Rs%.2f is below the entry zone (Rs%.2f-%.2f) — waiting",
                     card.contract, base, card.entry_low, card.entry_high)
            self._defer(card, f"Rs{base:.2f} below zone")
            return                       # NOT marked seen — may re-enter the zone

        # ENTRY-ZONE CEILING, the mirror image. A real basket LIMITs at
        # entry_high; a tape trading above it means that order rests unfilled
        # until price comes back into the zone. The old behaviour "filled" at
        # entry_high anyway — on 22-Jul it bought ₹143.72 while the market
        # traded ₹153.90, turning a losing entry into +₹521 of recorded profit.
        # Waiting (not burning the card) is exactly what the resting LIMIT does.
        if base > card.entry_high:
            log.info("paper: %s at Rs%.2f is above the entry zone (Rs%.2f-%.2f) — limit rests",
                     card.contract, base, card.entry_low, card.entry_high)
            self._defer(card, f"Rs{base:.2f} above zone")
            return                       # NOT marked seen — may pull back into the zone

        # CAPACITY IS TEMPORARY, so it must not burn the card. On 22-Jul the
        # highest-scoring signal the engine has ever produced (92.9) was offered
        # while the book was full, marked seen, and never reconsidered — by the
        # time a slot freed, the card was gone. Anything that can resolve on its
        # own while the card is still valid returns WITHOUT marking seen.
        # PER MODE, unlike the live throttle's global cap, and on purpose: the
        # paper book's product is EVIDENCE, and each mode's go-live/calibration
        # gate needs its own sample (scalp: 50 honest fills; intraday T1: 30).
        # With a shared cap, one positional row (no clock exit — it can hold a
        # slot for days) plus one intraday row would block every scalp fill,
        # and a 10-minute scalp hold outlives an intraday card's 480s validity
        # — each mode's sample would be thinned, and selection-biased toward
        # quiet tape, by the OTHER modes' holding times.
        # Shadow CLASS must match too: clean, floor-counterfactual and
        # late-counterfactual books each get their own per-mode slots. A
        # coarser hollow-vs-clean split let floor shadows occupy the bucket
        # and starve late shadows of fills (review finding) — starving either
        # hypothesis slows and biases its own 30-fill verdict.
        card_hollow = bool(getattr(card, "hollow_reason", None))
        card_class = shadow_class(card)
        open_now = [t for t in self.store.all()
                    if t.status in (TradeStatus.ENTERED, TradeStatus.PARTIAL)
                    and t.mode == card.mode and shadow_class(t) == card_class]
        # The runtime override, not the .env default: the live engine reads the
        # same overlay, and a simulation gated tighter than the thing it is
        # meant to model reports fewer trades than the system would have taken.
        cap = int(risk_limit_store.effective(self.cfg)["max_open_positions"])
        if cap > 0 and len(open_now) >= cap:
            log.info("paper: %s deferred — %d/%d %s position(s) open",
                     card.contract, len(open_now), cap, card.mode.value)
            self._defer(card, f"book full ({len(open_now)}/{cap} {card.mode.value})")
            return                       # NOT marked seen — a slot may free up

        lot = card.lot_size or 0
        if lot <= 0:
            log.info("paper: %s deferred — no lot size yet", card.contract)
            self._defer(card, "no lot size")
            return                       # NOT marked seen — instrument data may still arrive

        self._seen.add(card.id)          # committed: everything below always enters
        self._deferred.pop(card.id, None)
        self.filled += 1

        # entry_high is what a real basket would LIMIT at, so a fill above it is
        # not achievable; the floor above keeps it inside the zone.
        fill = round(min(base * (1 + self.cfg.paper_slippage_pct), card.entry_high), 2)

        lots = max(1, self.cfg.paper_lots or (card.suggested_lots or 1))
        # Same re-pricing the live journal does, or the simulation would grade a
        # ladder no real trade of this fill would have carried.
        params = ladder_params(self.cfg, card.mode)
        sl_pct, rr1, rr2 = params if params else (None, None, None)
        # The hollow tag rides the notes field, set AT FILL (not a follow-up
        # update): if the tag ever failed to land, is_hollow_row() would read
        # the row as clean and it would pollute every aggregate the floor's
        # evidence separation depends on. Atomic with the row, no window.
        t = self.store.create_from_signal(
            card, lots, fill, lot, product=None,
            disaster_pct=(self.cfg.premium_disaster_pct
                          if self.cfg.stop_primary == "underlying"
                          and self.cfg.trading_capital > 0 else None),
            quick_pct=self.cfg.quick_target_pct or None,
            sl_pct=sl_pct, rr1=rr1, rr2=rr2,
            notes=f"hollow: {card.hollow_reason}" if card_hollow else None,
        )
        log.info("paper: entered %s%s %d lot(s) @ Rs%s (signal %s)",
                 "HOLLOW " if card_hollow else "", card.contract, lots, fill, card.id)

        # STOP-BASIS PAIRED A/B (audit P1-5). STOP_PRIMARY was flipped twice on
        # single-trade evidence while the codebase's own rule demands 30. Every
        # CLEAN fill books a twin that differs in exactly one way: the OTHER
        # stop basis (disaster backstop present vs absent — the monitor derives
        # the whole premium-vs-underlying semantics from that one field). Same
        # entry, same ladder, same exit policy; the pair diverges only when the
        # 18% premium stop and the wide backstop disagree — which is precisely
        # the question. Twins ride the hollow: notes channel so every consumer
        # that grades the system's own decisions already excludes them, and
        # they bypass the capacity bucket by construction (1:1 with a clean
        # fill that already cleared it). Failure here must never undo the
        # clean fill — the twin is evidence, not the trade.
        if not card_hollow and self.cfg.stop_ab_paired and self.cfg.trading_capital > 0:
            other = "underlying" if self.cfg.stop_primary != "underlying" else "premium"
            try:
                self.store.create_from_signal(
                    card, lots, fill, lot, product=None,
                    disaster_pct=(self.cfg.premium_disaster_pct
                                  if other == "underlying" else None),
                    quick_pct=self.cfg.quick_target_pct or None,
                    sl_pct=sl_pct, rr1=rr1, rr2=rr2,
                    notes=(f"hollow: stopb: {other} — stop-basis A/B twin "
                           f"(live basis: {self.cfg.stop_primary})"),
                )
                log.info("paper: stop-A/B twin booked for %s (%s-primary arm)",
                         card.contract, other)
            except Exception:
                log.warning("paper: stop-A/B twin failed for %s — pair skipped",
                            card.contract, exc_info=True)
        return t

    # ---- exit --------------------------------------------------------------
    def run_once(self) -> None:
        now = int(time.time())
        ist_min, ist_day = _ist_minutes(now), _ist_date(now)
        self.sweep_expired(now)

        # Stall is clock-driven, so it alone could "exit" on a price frozen by
        # a feed blackout — disarm it while ticks are stale (same rule as the
        # live journal). getattr: unit-test fakes carry only `ticks`.
        age_fn = getattr(self.state, "last_tick_age", None)
        age = age_fn() if callable(age_fn) else None
        fresh = age is not None and age <= max(self.cfg.feed_watchdog_age_s or 150, 60)
        stall = self.cfg.stall_exit_minutes if fresh else 0

        def updater(trade) -> None:
            current = None
            if trade.token is not None:
                current = self.state.ticks.get(trade.token, {}).get("last_price")
            # Same-day closed rows keep being OBSERVED (post_close_* extremes):
            # the early-derisk aftermath ledger scores each breakeven lock-out
            # by what the premium did next, which is unknowable without this.
            if trade.status.value not in ("entered", "partial"):
                monitor.track_reversible(trade, current)
                return
            snap = self.state.underlying_snapshot(trade.symbol)
            spot = snap.ltp if snap else None
            # A scalp thesis is stale in minutes, not three-quarters of an hour.
            sm = min(stall, 10) if (stall and trade.mode.value == "scalp") else stall
            monitor.evaluate(trade, current, spot, ist_min, ist_day,
                             stall_minutes=sm,
                             early_derisk_pct=self.cfg.early_derisk_mfe_pct)

        self.store.apply_monitor(updater, include_reversible=True)

        # Book half at the early target before considering exits. Without this
        # the simulator would record the stop-to-entry benefit but never the
        # banked profit, understating exactly the strategy being tested.
        for trade in self.store.all():
            if (
                trade.t0_hit
                and trade.status is TradeStatus.ENTERED     # not already part-booked
                and trade.lots > 1
                and trade.current_premium
            ):
                fill = round(max(0.05, trade.current_premium * (1 - self.cfg.paper_slippage_pct)), 2)
                if self.store.book_partial(trade.id, fill, 0.5) is not None:
                    log.info("paper: booked half of %s @ Rs%s (early target)", trade.contract, fill)

        # Closing happens outside apply_monitor: that call holds the store lock
        # while iterating, and closing takes the same lock to write.
        # quick_bank joins the trigger set only when the policy flag is on —
        # until then the exit_ab block below measures it as a counterfactual.
        triggers = (_EXIT_TRIGGERS | {"quick_bank"}
                    if self.cfg.quick_bank_single_lot else _EXIT_TRIGGERS)
        for trade in self.store.all():
            reason = monitor.auto_close_trigger(trade, triggers)
            if reason is None:
                continue
            px = trade.current_premium
            if not px or px <= 0:
                continue
            # Slippage against us on the way out too.
            fill = round(max(0.05, px * (1 - self.cfg.paper_slippage_pct)), 2)
            closed = self.store.auto_close(trade.id, fill, reason, price_source="simulated")
            if closed is not None:
                net = chg.net_pnl(trade.entry_premium, fill, trade.quantity)
                log.info("paper: exited %s on %s @ Rs%s — net Rs%s",
                         trade.contract, reason, fill, net)


def summarize(store: TradeStore, exit_slippage_pct: float = 0.0,
              quick_bank_live: bool = False) -> dict:
    """Net-of-charges performance of the simulated book.

    Aggregates count the HONEST-FILL ERA ONLY. Rows entered before the
    slippage/trigger-cap fix landed (23-Jul-2026 IST) are kept in the ledger
    for the record, flagged "inflated", and excluded from every statistic —
    a win rate propped up by fictional fills is worse than no win rate.

    `exit_slippage_pct` prices the exit-A/B counterfactual's quick-target fill
    (the route passes cfg.paper_slippage_pct; the 0.0 default keeps this
    function pure for tests). `quick_bank_live` marks that banking IS the live
    policy, in which case the ratchet counterfactual is unobservable.
    """
    closed = [t for t in store.all() if t.status is TradeStatus.EXITED and t.exit_premium]
    rows = []
    inflated = []
    hollow_rows = []
    late_rows = []
    refire_rows = []
    stopb_rows = []
    setup_rows = []
    for t in closed:
        qty = t.initial_quantity or t.quantity
        net = chg.net_pnl(t.entry_premium, t.exit_premium, qty)
        deployed = t.entry_premium * qty
        honest = t.entered_at >= HONEST_FILLS_FROM
        hollow = is_hollow_row(t)
        # Four shadow ledgers, one tag channel: "hollow: late: ..." rows test
        # the 14:15 cutoff hypothesis, "hollow: refire: ..." rows test the
        # re-fire guard, "hollow: stopb: ..." rows are the stop-basis A/B
        # twins (paired below, never aggregated alone), plain "hollow: ..."
        # rows test the volume/OI participation floor. Each verdict block
        # must stay pure.
        cls = shadow_class(t)
        row = {
            "id": t.id, "contract": t.contract, "direction": t.direction.value,
            "entered_at": t.entered_at, "exited_at": t.exited_at,
            "entry": t.entry_premium, "exit": t.exit_premium, "quantity": qty,
            "reason": t.auto_close_reason,
            "gross_pnl": round((t.exit_premium - t.entry_premium) * qty, 2),
            "charges": chg.charges(t.entry_premium, t.exit_premium, qty, 2),
            "net_pnl": net,
            "return_pct": round(net / deployed * 100, 2) if deployed else 0.0,
            "era": "honest" if honest else "inflated (pre-honest-fill)",
            "hollow": hollow,
            "shadow_class": cls,
            "mode": t.mode.value,
        }
        if cls == "setup":
            # Per-setup grouping key, parsed from the atomic fill tag
            # ("hollow: setup: vwap_cross — ..."). The audit demands
            # per-setup expectancy from day one — a blended setups number
            # would hide which detector earns its keep.
            try:
                row["setup"] = (t.notes or "").split("setup:", 1)[1].strip().split(" ", 1)[0]
            except Exception:
                row["setup"] = "unknown"
        # The books: the system's own decisions (aggregated), each shadow
        # class (its own verdict block below), and pre-honest-era history
        # (flagged, counted nowhere).
        (late_rows if (honest and cls == "late")
         else refire_rows if (honest and cls == "refire")
         else stopb_rows if (honest and cls == "stopb")
         else setup_rows if (honest and cls == "setup")
         else hollow_rows if (honest and hollow)
         else rows if honest else inflated).append(row)
    wins = [r for r in rows if r["net_pnl"] > 0]
    losses = [r for r in rows if r["net_pnl"] < 0]
    net_total = round(sum(r["net_pnl"] for r in rows), 2)
    # Open positions split the same way every other stat is: the clean book's
    # count is the headline number, hollow (vetoed-card) open fills go to the
    # hollow block. Blending them would inflate the apparent live book by the
    # engine's own counterfactuals, next to a clean-only "Closed" count.
    open_all = [t for t in store.all()
                if t.status in (TradeStatus.ENTERED, TradeStatus.PARTIAL)]
    open_late = [t for t in open_all if shadow_class(t) == "late"]
    open_refire = [t for t in open_all if shadow_class(t) == "refire"]
    open_setup = [t for t in open_all if shadow_class(t) == "setup"]
    open_hollow = [t for t in open_all if shadow_class(t) == "floor"]
    open_clean = [t for t in open_all if shadow_class(t) is None]

    # ---- 1-lot exit-policy A/B: trailing ratchet vs bank-the-quick-target ---
    # The two policies are IDENTICAL until the quick target trades, so the
    # banked variant's exit is reconstructable from each recorded row: the
    # premium observed at the t0 cross (t0_cross_premium — what a banking exit
    # would actually fill at, gaps included) with the quick_target level as the
    # fallback for rows recorded before that field existed. A paired comparison
    # on the same trades — no second book, no sampling noise between arms.
    # Single-lot rows only (a multi-lot trade already books half at the quick
    # target), honest era, counterfactuals excluded, intraday/scalp only
    # (banking a POSITIONAL thesis at +12% forfeits the multi-day move that
    # mode exists to ride — the retro book's one positional runner was +60%
    # ridden vs +12% banked; no sample size makes that trade).
    #
    # Rows the live quick_bank trigger itself closed are EXCLUDED from both
    # arms: their ratchet path was never observed, so crediting their banked
    # exit to the "ratchet" arm after the flag is turned back off would let
    # the policy grade itself. They are counted separately for transparency.
    ab_all = [t for t in closed
              if t.entered_at >= HONEST_FILLS_FROM and not is_hollow_row(t)
              and t.mode.value in ("intraday", "scalp")
              and t.initial_quantity == t.lot_size]
    banked_live = [t for t in ab_all if t.auto_close_reason == "quick_bank"]
    ab_rows = [t for t in ab_all if t.auto_close_reason != "quick_bank"]
    exit_ab = None
    if ab_all and quick_bank_live:
        exit_ab = {
            "policy_live": "quick_bank", "n": len(ab_all),
            "note": ("Banking is the live policy — the ratchet counterfactual is "
                     "unobservable. Flip QUICK_BANK_SINGLE_LOT off to resume the A/B."),
        }
    elif ab_rows:
        pairs = []
        for t in ab_rows:
            qty = t.initial_quantity or t.quantity
            actual = chg.net_pnl(t.entry_premium, t.exit_premium, qty)
            diverged = bool(t.t0_hit and t.quick_target)
            if diverged:
                cross = t.t0_cross_premium or t.quick_target
                vexit = round(cross * (1 - exit_slippage_pct), 2)
                variant = chg.net_pnl(t.entry_premium, vexit, qty)
            else:
                variant = actual         # below the quick target the policies agree
            pairs.append((actual, variant, diverged))
        n, n_div = len(pairs), sum(1 for *_, d in pairs if d)
        a_tot = round(sum(a for a, _, _ in pairs), 2)
        v_tot = round(sum(v for _, v, _ in pairs), 2)
        delta = round(v_tot - a_tot, 2)
        if n_div < 30:
            verdict = (f"{n_div} diverged fill(s) — evidence gathering; "
                       "decide at 30+, not before.")
        elif delta > 0:
            verdict = "Banking wins on this sample — consider QUICK_BANK_SINGLE_LOT=true."
        elif delta < 0:
            verdict = "The ratchet wins on this sample — keep it."
        else:
            verdict = "Dead heat on this sample."
        exit_ab = {
            "policy_live": "ratchet",
            "n": n, "n_diverged": n_div,
            # Fills the live quick_bank trigger closed during an earlier flag-on
            # period — visible, but in neither arm (their ratchet path was
            # never observed).
            "banked_live_excluded": len(banked_live),
            "ratchet": {"net_pnl": a_tot, "expectancy": round(a_tot / n, 2),
                        "win_rate": round(100 * sum(1 for a, _, _ in pairs if a > 0) / n, 1)},
            "quick_bank": {"net_pnl": v_tot, "expectancy": round(v_tot / n, 2),
                           "win_rate": round(100 * sum(1 for _, v, _ in pairs if v > 0) / n, 1)},
            "delta_net": delta,
            "verdict": verdict,
        }

    # ---- STOP-BASIS PAIRED A/B (audit P1-5) ---------------------------------
    # STOP_PRIMARY was flipped twice on single-trade evidence (underlying ->
    # premium -> ... , 21-Jul, n=1 both times) while the codebase's own rule
    # demands 30 samples for far smaller decisions. Every clean fill books a
    # twin identical except for the stop basis (see consider()); pairing them
    # by signal id compares the two bases on the SAME trades — no sampling
    # noise between arms. A pair settles when BOTH legs close (the wide-
    # backstop arm can outlive the premium-stop arm by hours); it diverges
    # when the two stops actually produced different exits. Verdict at 30+
    # DIVERGED pairs — agreeing pairs carry no information about the choice.
    stopb_closed = [t for t in closed if shadow_class(t) == "stopb"
                    and t.entered_at >= HONEST_FILLS_FROM and t.signal_id]
    twin_by_signal = {}
    for t in stopb_closed:
        twin_by_signal.setdefault(t.signal_id, t)
    stop_ab = None
    ab_pairs = []
    for c in closed:
        if shadow_class(c) is not None or c.entered_at < HONEST_FILLS_FROM:
            continue
        tw = twin_by_signal.get(c.signal_id) if c.signal_id else None
        if tw is None:
            continue
        c_net = chg.net_pnl(c.entry_premium, c.exit_premium,
                            c.initial_quantity or c.quantity)
        t_net = chg.net_pnl(tw.entry_premium, tw.exit_premium,
                            tw.initial_quantity or tw.quantity)
        # The twin's notes record which basis IT ran, so pairs stay correctly
        # labelled even across a future STOP_PRIMARY flip mid-history.
        twin_is_underlying = (tw.notes or "").startswith("hollow: stopb: underlying")
        prem_net, und_net = (c_net, t_net) if twin_is_underlying else (t_net, c_net)
        ab_pairs.append({
            "premium": prem_net, "underlying": und_net,
            "diverged": (c.auto_close_reason != tw.auto_close_reason
                         or abs((c.exit_premium or 0.0) - (tw.exit_premium or 0.0)) > 0.01),
        })
    # Twins whose pair has not settled yet (either leg still open) stay
    # visible — a backstop arm still riding must not vanish from the count.
    # Same honest-era universe as the settled set (review catch): if the
    # cutoff is ever re-baselined, a pre-cutoff pair must drop out of BOTH
    # sets, not linger as a phantom "pending" that can never clear.
    settled_twins = {t.signal_id for t in stopb_closed
                     if any(c.signal_id == t.signal_id and shadow_class(c) is None
                            for c in closed)}
    all_twin_ids = {t.signal_id for t in store.all()
                    if shadow_class(t) == "stopb" and t.signal_id
                    and t.entered_at >= HONEST_FILLS_FROM}
    ab_pending = len(all_twin_ids - settled_twins)
    if ab_pairs or ab_pending:
        n_pairs, n_div = len(ab_pairs), sum(1 for p in ab_pairs if p["diverged"])
        p_tot = round(sum(p["premium"] for p in ab_pairs), 2)
        u_tot = round(sum(p["underlying"] for p in ab_pairs), 2)
        delta = round(u_tot - p_tot, 2)
        if not ab_pairs:
            verdict = "First pair(s) still open — a pair settles when both arms close."
        elif n_div < 30:
            verdict = (f"{n_div} diverged pair(s) — evidence gathering. The 30-pair "
                       "rule applies to THIS knob especially: it was flipped on "
                       "n=1 twice.")
        elif delta > 0:
            verdict = "Underlying-primary wins this sample — consider STOP_PRIMARY=underlying."
        elif delta < 0:
            verdict = "Premium-primary wins this sample — consider STOP_PRIMARY=premium."
        else:
            verdict = "Dead heat on this sample."
        stop_ab = {
            "n": n_pairs, "n_diverged": n_div, "pending": ab_pending,
            "premium_stop": {
                "net_pnl": p_tot,
                "expectancy": round(p_tot / n_pairs, 2) if n_pairs else 0.0,
                "win_rate": (round(100 * sum(1 for p in ab_pairs if p["premium"] > 0)
                                   / n_pairs, 1) if n_pairs else 0.0)},
            "underlying_stop": {
                "net_pnl": u_tot,
                "expectancy": round(u_tot / n_pairs, 2) if n_pairs else 0.0,
                "win_rate": (round(100 * sum(1 for p in ab_pairs if p["underlying"] > 0)
                                   / n_pairs, 1) if n_pairs else 0.0)},
            "delta_net": delta,
            "verdict": verdict,
        }

    # ---- THE OVERNIGHT-HOLD LEDGER (positional) -----------------------------
    # The 29-Jul forensic's honest gap: most of a 58->120 option move happened
    # in an overnight gap no intraday system can touch — the only way to own a
    # gap is to be holding when it opens. This block grades exactly that bet:
    # positional rows that survived an IST day boundary, with the EVENING
    # subset (entries at/after 14:30, the same minute the gap caution fires)
    # split out, because "hold into close when positional score > X" is the
    # candidate pattern. Every fill already cleared the positional gate (72),
    # so X lives ABOVE it — each row carries its score and its next-session
    # first print so X gets picked from this ledger, not guessed.
    def _on_stats(rs: list[dict]) -> dict:
        return {
            "trades": len(rs),
            "net_pnl": round(sum(r["net_pnl"] for r in rs), 2),
            "expectancy": round(sum(r["net_pnl"] for r in rs) / len(rs), 2),
            "win_rate": round(100 * sum(1 for r in rs if r["net_pnl"] > 0) / len(rs), 1),
        }

    on_rows = []
    for t in closed:
        if (
            t.entered_at < HONEST_FILLS_FROM or is_hollow_row(t)
            or t.mode.value != "positional" or not t.exited_at
            or _ist_date(t.exited_at) <= _ist_date(t.entered_at)
        ):
            continue
        qty = t.initial_quantity or t.quantity
        on_rows.append({
            "contract": t.contract, "direction": t.direction.value,
            "score": t.entry_score,
            "entered_at": t.entered_at, "exited_at": t.exited_at,
            "evening": _ist_minutes(t.entered_at) >= EVENING_MIN,
            "entry": t.entry_premium,
            "next_open": t.next_open_premium,
            # entry -> next-session first print: the gap component in
            # isolation. None on rows recorded before the latch existed.
            "overnight_move_pct": (
                round((t.next_open_premium - t.entry_premium)
                      / t.entry_premium * 100, 1)
                if t.next_open_premium and t.entry_premium else None),
            "net_pnl": chg.net_pnl(t.entry_premium, t.exit_premium, qty),
            "reason": t.auto_close_reason,
        })
    evening_rows = [r for r in on_rows if r["evening"]]

    # ---- EARLY-DERISK AFTERMATH: is the +5% lock earning its keep? ----------
    # The lock went live 3-Aug without a paired A/B (its clean counterfactual
    # became unobservable the moment it started ending trades at entry). This
    # is the honest substitute: every trade the lock ENDED at ~breakeven keeps
    # being observed through the same-day post-close window, and what the
    # premium did next scores the lock — fell to where the old stop lived
    # (crash avoided: the lock saved that loss) vs ran to the quick target
    # (runner escaped: the lock forfeited that gain). Window-bounded (rest of
    # the session) and same-day only — stated, not hidden.
    derisked = [t for t in closed
                if t.entered_at >= HONEST_FILLS_FROM and shadow_class(t) is None
                and t.mode.value in ("intraday", "scalp")
                and any(e.kind == "early_derisk" for e in t.events)]
    locked_out = [t for t in derisked
                  if t.auto_close_reason == "stop" and t.entry_premium
                  and abs(t.exit_premium - t.entry_premium) <= t.entry_premium * 0.015]
    esc, avoided, noise, unobserved = [], [], [], []
    forfeited_val = avoided_val = 0.0
    for t in locked_out:
        qty = t.initial_quantity or t.quantity
        up, dn = t.post_close_mfe, t.post_close_mae
        if up is None and dn is None:
            unobserved.append(t)             # closed too near the bell to observe
            continue
        ran_to = t.quick_target or t.entry_premium * 1.12
        if up is not None and up >= ran_to:
            esc.append(t)
            forfeited_val += (up - t.exit_premium) * qty
        elif dn is not None and dn <= t.entry_premium * 0.92:
            avoided.append(t)
            avoided_val += (t.exit_premium - dn) * qty
        else:
            noise.append(t)
    derisk_aftermath = {
        "armed": len(derisked),              # lock fired at least once
        "locked_out": len(locked_out),       # ended at ~breakeven by the lock
        "runner_escaped": len(esc),
        "crash_avoided": len(avoided),
        "noise": len(noise),
        "unobserved": len(unobserved),
        "forfeited": round(forfeited_val, 2),   # upside seen AFTER lock-outs
        "avoided": round(avoided_val, 2),       # downside dodged by lock-outs
        "note": ("Same-session observation window only — the score is a floor "
                 "on both sides, not the full counterfactual."),
    } if derisked else None

    return {
        "trades": len(rows),
        "open": len(open_clean),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(rows), 1) if rows else 0.0,
        "gross_pnl": round(sum(r["gross_pnl"] for r in rows), 2),
        "charges": round(sum(r["charges"] for r in rows), 2),
        "net_pnl": net_total,
        "avg_win": round(sum(r["net_pnl"] for r in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(r["net_pnl"] for r in losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(net_total / len(rows), 2) if rows else 0.0,
        "by_mode": {
            m: {
                "trades": len(g),
                "net_pnl": round(sum(r["net_pnl"] for r in g), 2),
                "expectancy": round(sum(r["net_pnl"] for r in g) / len(g), 2),
                "win_rate": round(100 * sum(1 for r in g if r["net_pnl"] > 0) / len(g), 1),
            }
            for m, g in (
                (m, [r for r in rows if r["mode"] == m])
                for m in sorted({r["mode"] for r in rows})
            )
        },
        "by_reason": {
            r: sum(1 for x in rows if x["reason"] == r)
            for r in sorted({x["reason"] for x in rows if x["reason"]})
        },
        "note": ("Simulated. Fills carry slippage and the full Zerodha charge "
                 "schedule, but assume your order always fills at the tape — a "
                 "real order can miss a fast move entirely."
                 + (f" {len(inflated)} pre-23-Jul row(s) are shown but excluded "
                    "from every aggregate: their fills were inflated."
                    if inflated else "")),
        "inflated_trades": len(inflated),
        # THE FLOOR'S OWN VERDICT: net expectancy of the fills the volume/OI
        # participation floor vetoed. Negative and staying negative = the floor
        # is earning its keep; positive over a real sample = lower the floors,
        # with this ledger as the evidence. None until the first hollow fill.
        "hollow": {
            "trades": len(hollow_rows),
            "open": len(open_hollow),
            "net_pnl": round(sum(r["net_pnl"] for r in hollow_rows), 2),
            "expectancy": (round(sum(r["net_pnl"] for r in hollow_rows) / len(hollow_rows), 2)
                           if hollow_rows else 0.0),
            "win_rate": (round(100 * sum(1 for r in hollow_rows if r["net_pnl"] > 0)
                               / len(hollow_rows), 1) if hollow_rows else 0.0),
        } if (hollow_rows or open_hollow) else None,
        # THE 14:15-CUTOFF HYPOTHESIS LEDGER (audit P0-2): fills of cards the
        # late-entry cutoff vetoed, 14:15-15:10 only. Live n=7 said such cards
        # always lose; the 43-session replay said hour-15 is the BEST hour on
        # the underlying (but it cannot see theta). These fills pay theta.
        # Verdict at 30+ fills: positive expectancy -> retire the cutoff.
        "late_shadow": {
            "trades": len(late_rows),
            "open": len(open_late),
            "net_pnl": round(sum(r["net_pnl"] for r in late_rows), 2),
            "expectancy": (round(sum(r["net_pnl"] for r in late_rows) / len(late_rows), 2)
                           if late_rows else 0.0),
            "win_rate": (round(100 * sum(1 for r in late_rows if r["net_pnl"] > 0)
                               / len(late_rows), 1) if late_rows else 0.0),
        } if (late_rows or open_late) else None,
        # THE RE-FIRE GUARD'S OWN VERDICT (03-Aug): fills of cards the guard
        # refused because the same thesis stopped/invalidated within the 2h
        # window. Born from n=2 losing re-fires (28-Jul); first known blocked
        # winner 03-Aug (24550 CE, +6-9% unmeasured). Negative expectancy at
        # 30+ fills = the guard earns its keep; positive = shorten or retire
        # SIGNAL_REFIRE_GUARD_S — this ledger decides, not anecdotes.
        "refire_shadow": {
            "trades": len(refire_rows),
            "open": len(open_refire),
            "net_pnl": round(sum(r["net_pnl"] for r in refire_rows), 2),
            "expectancy": (round(sum(r["net_pnl"] for r in refire_rows) / len(refire_rows), 2)
                           if refire_rows else 0.0),
            "win_rate": (round(100 * sum(1 for r in refire_rows if r["net_pnl"] > 0)
                               / len(refire_rows), 1) if refire_rows else 0.0),
        } if (refire_rows or open_refire) else None,
        # THE SETUP DETECTORS' LEDGER (audit P1-4): fills of cards the
        # structural detectors booked — candidates the score engine cannot
        # see. Per-setup verdicts at 30+ fills each; parameters are frozen
        # at registration, and changing them resets a setup's count.
        # None until the first setup fill.
        "setup_shadow": {
            "trades": len(setup_rows),
            "open": len(open_setup),
            "net_pnl": round(sum(r["net_pnl"] for r in setup_rows), 2),
            "expectancy": (round(sum(r["net_pnl"] for r in setup_rows) / len(setup_rows), 2)
                           if setup_rows else 0.0),
            "win_rate": (round(100 * sum(1 for r in setup_rows if r["net_pnl"] > 0)
                               / len(setup_rows), 1) if setup_rows else 0.0),
            "by_setup": {
                s: {
                    "trades": len(g),
                    "net_pnl": round(sum(r["net_pnl"] for r in g), 2),
                    "expectancy": round(sum(r["net_pnl"] for r in g) / len(g), 2),
                    "win_rate": round(100 * sum(1 for r in g if r["net_pnl"] > 0) / len(g), 1),
                }
                for s, g in (
                    (s, [r for r in setup_rows if r.get("setup") == s])
                    for s in sorted({r.get("setup") or "unknown" for r in setup_rows})
                ) if g
            },
        } if (setup_rows or open_setup) else None,
        # THE EXIT-POLICY A/B (see the block above): same trades, two exits.
        "exit_ab": exit_ab,
        # THE STOP-BASIS A/B (audit P1-5, see the block above): same trades,
        # premium stop vs underlying-invalidation-with-disaster-backstop.
        # None until the first twin books.
        "stop_ab": stop_ab,
        # THE OVERNIGHT-HOLD LEDGER (see the block above): did holding a
        # positional thesis through the close pay, and did the late-day
        # entries — the deliberate gap bets — pay more? None until the first
        # positional row survives a day boundary in the honest era.
        "overnight": {
            **_on_stats(on_rows),
            "evening": _on_stats(evening_rows) if evening_rows else None,
            "rows": on_rows,
        } if on_rows else None,
        # THE EARLY-DERISK AFTERMATH (see the block above): what premiums did
        # AFTER the +5% lock ended a trade at breakeven — the lock's scoreboard.
        "derisk_aftermath": derisk_aftermath,
        # Shadow and inflated rows LAST, visibly flagged — context, not evidence.
        "rows": rows + hollow_rows + late_rows + refire_rows + setup_rows + stopb_rows + inflated,
    }
