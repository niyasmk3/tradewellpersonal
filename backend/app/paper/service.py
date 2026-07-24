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
        open_now = [t for t in self.store.all()
                    if t.status in (TradeStatus.ENTERED, TradeStatus.PARTIAL)
                    and t.mode == card.mode]
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
        t = self.store.create_from_signal(
            card, lots, fill, lot, product=None,
            disaster_pct=(self.cfg.premium_disaster_pct
                          if self.cfg.stop_primary == "underlying"
                          and self.cfg.trading_capital > 0 else None),
            quick_pct=self.cfg.quick_target_pct or None,
            sl_pct=sl_pct, rr1=rr1, rr2=rr2,
        )
        log.info("paper: entered %s %d lot(s) @ Rs%s (signal %s)",
                 card.contract, lots, fill, card.id)
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
            snap = self.state.underlying_snapshot(trade.symbol)
            spot = snap.ltp if snap else None
            # A scalp thesis is stale in minutes, not three-quarters of an hour.
            sm = min(stall, 10) if (stall and trade.mode.value == "scalp") else stall
            monitor.evaluate(trade, current, spot, ist_min, ist_day,
                             stall_minutes=sm)

        self.store.apply_monitor(updater)

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
        for trade in self.store.all():
            reason = monitor.auto_close_trigger(trade, _EXIT_TRIGGERS)
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


def summarize(store: TradeStore) -> dict:
    """Net-of-charges performance of the simulated book.

    Aggregates count the HONEST-FILL ERA ONLY. Rows entered before the
    slippage/trigger-cap fix landed (23-Jul-2026 IST) are kept in the ledger
    for the record, flagged "inflated", and excluded from every statistic —
    a win rate propped up by fictional fills is worse than no win rate.
    """
    closed = [t for t in store.all() if t.status is TradeStatus.EXITED and t.exit_premium]
    rows = []
    inflated = []
    for t in closed:
        qty = t.initial_quantity or t.quantity
        net = chg.net_pnl(t.entry_premium, t.exit_premium, qty)
        deployed = t.entry_premium * qty
        honest = t.entered_at >= HONEST_FILLS_FROM
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
        }
        (rows if honest else inflated).append(row)
    wins = [r for r in rows if r["net_pnl"] > 0]
    losses = [r for r in rows if r["net_pnl"] < 0]
    net_total = round(sum(r["net_pnl"] for r in rows), 2)
    open_rows = [t for t in store.all()
                 if t.status in (TradeStatus.ENTERED, TradeStatus.PARTIAL)]
    return {
        "trades": len(rows),
        "open": len(open_rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(rows), 1) if rows else 0.0,
        "gross_pnl": round(sum(r["gross_pnl"] for r in rows), 2),
        "charges": round(sum(r["charges"] for r in rows), 2),
        "net_pnl": net_total,
        "avg_win": round(sum(r["net_pnl"] for r in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(r["net_pnl"] for r in losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(net_total / len(rows), 2) if rows else 0.0,
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
        # Inflated rows LAST, visibly flagged — history, not evidence.
        "rows": rows + inflated,
    }
