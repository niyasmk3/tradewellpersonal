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
from app.state import MarketState
from app.trades import monitor
from app.trades.models import TradeStatus
from app.trades.store import TradeStore

log = logging.getLogger("tradewell.paper")

# Every plan trigger closes a paper position — unlike the live journal, where
# which triggers auto-close is a user setting. A simulation that needs a human
# to close it is not a simulation.
_EXIT_TRIGGERS = {"stop", "target1", "target2", "invalidation", "time_exit"}


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
        live = None
        if card.token is not None:
            live = self.state.ticks.get(card.token, {}).get("last_price")
        base = live or card.ref_entry_premium or card.entry_high
        if not base or base <= 0:
            return                       # no tick yet — retry next cycle

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
            return                       # NOT marked seen — may re-enter the zone

        self._seen.add(card.id)          # marked even if skipped below: never retried

        open_now = [t for t in self.store.all()
                    if t.status in (TradeStatus.ENTERED, TradeStatus.PARTIAL)]
        cap = self.cfg.signal_max_open_positions
        if cap > 0 and len(open_now) >= cap:
            log.info("paper: skipping %s — %d position(s) already open", card.contract, len(open_now))
            return

        lot = card.lot_size or 0
        if lot <= 0:
            log.info("paper: skipping %s — no lot size", card.contract)
            return

        # entry_high is what a real basket would LIMIT at, so a fill above it is
        # not achievable; the floor above keeps it inside the zone.
        fill = round(min(base * (1 + self.cfg.paper_slippage_pct), card.entry_high), 2)

        lots = max(1, self.cfg.paper_lots or (card.suggested_lots or 1))
        t = self.store.create_from_signal(card, lots, fill, lot, product=None)
        log.info("paper: entered %s %d lot(s) @ Rs%s (signal %s)",
                 card.contract, lots, fill, card.id)
        return t

    # ---- exit --------------------------------------------------------------
    def run_once(self) -> None:
        now = int(time.time())
        ist_min, ist_day = _ist_minutes(now), _ist_date(now)

        def updater(trade) -> None:
            current = None
            if trade.token is not None:
                current = self.state.ticks.get(trade.token, {}).get("last_price")
            snap = self.state.underlying_snapshot(trade.symbol)
            spot = snap.ltp if snap else None
            monitor.evaluate(trade, current, spot, ist_min, ist_day)

        self.store.apply_monitor(updater)

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
            closed = self.store.auto_close(trade.id, fill, reason)
            if closed is not None:
                net = chg.net_pnl(trade.entry_premium, fill, trade.quantity)
                log.info("paper: exited %s on %s @ Rs%s — net Rs%s",
                         trade.contract, reason, fill, net)


def summarize(store: TradeStore) -> dict:
    """Net-of-charges performance of the simulated book."""
    closed = [t for t in store.all() if t.status is TradeStatus.EXITED and t.exit_premium]
    rows = []
    for t in closed:
        qty = t.initial_quantity or t.quantity
        net = chg.net_pnl(t.entry_premium, t.exit_premium, qty)
        deployed = t.entry_premium * qty
        rows.append({
            "id": t.id, "contract": t.contract, "direction": t.direction.value,
            "entered_at": t.entered_at, "exited_at": t.exited_at,
            "entry": t.entry_premium, "exit": t.exit_premium, "quantity": qty,
            "reason": t.auto_close_reason,
            "gross_pnl": round((t.exit_premium - t.entry_premium) * qty, 2),
            "charges": chg.charges(t.entry_premium, t.exit_premium, qty, 2),
            "net_pnl": net,
            "return_pct": round(net / deployed * 100, 2) if deployed else 0.0,
        })
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
                 "real order can miss a fast move entirely."),
        "rows": rows,
    }
