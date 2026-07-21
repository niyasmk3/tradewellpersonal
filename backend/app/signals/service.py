"""Bridges live MarketState into the (stateless) SignalEngine + SignalStore,
evaluating every enabled trading mode for every signal symbol.
"""
from __future__ import annotations

import logging
import time

from app.config import Settings
from app.kite.instruments import chain_key
from app.market import calendar as mcal
from app.market.candles import TIMEFRAME_SECONDS
from app.market.indicators import compute_snapshot
from app.news.store import news_store
from app.signals.engine import SignalEngine
from app.signals.modes import ModeProfile, build_profiles
from app.signals.models import (
    Action,
    Bias,
    MarketStatus,
    Regime,
    SignalResponse,
)
from app.signals.store import RiskState, SignalStore, ThrottleConfig
from app.state import MarketState
from app.trades.store import trade_store

log = logging.getLogger("tradewell.signals")


def _option_lot_size(token: int | None) -> int:
    """Lot size of the exact option contract behind `token`, or 0.

    Matched by INSTRUMENT TOKEN only — never by strike. A token identifies one
    contract unambiguously, whereas a strike match across universes once handed
    a positional card the weekly contract at the same strike. Imported lazily
    and failure-tolerant: this is a display refinement on the live signal path
    and must never be able to stop a card being issued.
    """
    if not token:
        return 0
    try:
        from app.services import feed

        builder = getattr(feed, "chain_builder", None)
        if builder is None:
            return 0
        for universe in builder.universes.values():
            for sp in universe.strikes.values():
                if sp.ce_token == token:
                    return int(sp.ce_lot_size or 0)
                if sp.pe_token == token:
                    return int(sp.pe_lot_size or 0)
    except Exception:  # pragma: no cover - never break signal generation
        log.debug("option lot-size lookup failed", exc_info=True)
    return 0


class SignalService:
    def __init__(self, cfg: Settings, state: MarketState, store: SignalStore) -> None:
        self.cfg = cfg
        self.state = state
        self.store = store
        self.engine = SignalEngine(cfg)
        self.profiles = build_profiles(cfg)
        self.throttle = ThrottleConfig(
            max_per_day=cfg.signal_max_per_day,
            min_gap_s=cfg.signal_min_gap_s,
            cooldown_s=cfg.signal_cooldown_s,
            flip_guard_s=cfg.signal_flip_guard_s,
            max_consecutive_losses=cfg.signal_max_consecutive_losses,
            daily_loss_limit=cfg.signal_daily_loss_limit,
            max_open_positions=cfg.signal_max_open_positions,
            max_open_drawdown=cfg.signal_max_open_drawdown,
        )

    def _risk_state(self, now: int) -> RiskState:
        """Today's outcome, read from the journal — realised AND still open.

        Feeds the circuit breakers: the engine must go quiet after a bad run,
        not keep talking. Open positions are included because a loss you are
        still holding is not a smaller loss than one you have booked, and the
        realised-only view is blind precisely while you sit in a drawdown.
        """
        today = (now + 19800) // 86400
        trades = trade_store.all()
        closed = [
            t for t in trades
            if t.status.value == "exited" and t.exited_at
            and (t.exited_at + 19800) // 86400 == today
        ]
        closed.sort(key=lambda t: t.exited_at or 0)
        realized = sum(t.realized_pnl or 0.0 for t in closed)
        streak = 0
        for t in reversed(closed):                 # most recent backwards
            if (t.realized_pnl or 0.0) < 0:
                streak += 1
            else:
                break

        # Positional rows carry overnight — that is real money still at risk.
        # INTRADAY rows must not: Zerodha auto-squares MIS around 15:20 IST, and
        # nothing here ever closes a trade by itself, so a row the user forgot to
        # mark exited would otherwise count forever. With max_open_positions
        # defaulting to 2, two such ghosts would silence the engine permanently.
        open_trades = [
            t for t in trades
            if t.status.value in ("entered", "partial")
            and not (t.mode.value == "intraday"
                     and (t.entered_at + 19800) // 86400 < today)
        ]
        open_pnl = sum(t.pnl or 0.0 for t in open_trades)

        return RiskState(
            consecutive_losses=streak,
            realized_today=realized,
            open_pnl=open_pnl,
            open_positions=len(open_trades),
        )

    def _apply_sizing(self, resp: SignalResponse, symbol: str) -> None:
        """Suggest lots from a risk budget. Never guesses: with no configured
        capital the card simply carries no suggestion."""
        card = resp.signal
        if card is None:
            return
        capital = self.cfg.trading_capital
        meta = self.state.underlyings.get(symbol.upper())
        # Prefer THIS option contract's own lot size over the future's: NSE
        # applies lot revisions to newly listed series while live contracts
        # keep the old size, so the two disagree for weeks. The Kite basket
        # already resolves it this way, and the rupee figures the UI shows must
        # match the quantity Kite actually receives.
        lot = _option_lot_size(card.token) or (meta.lot_size if meta and meta.lot_size else 0)
        entry = card.ref_entry_premium or card.entry_high
        # Size against the stop that ACTUALLY ends the trade. With the index
        # invalidation primary, that is the disaster backstop — a wider stop is
        # more rupees per lot, so the same risk budget buys fewer lots. Sizing
        # off the narrower premium_sl would understate risk by ~2.5x.
        operative_sl = card.disaster_sl or card.premium_sl
        per_unit_risk = max(0.0, entry - operative_sl)

        # Contract/account facts are attached BEFORE the suggestion returns
        # early: the UI needs the lot size to show rupee outcomes even when no
        # capital is configured, and "no suggestion" must not mean "no numbers".
        card.lot_size = lot or None
        card.trading_capital = capital or None
        card.daily_loss_limit = self.cfg.signal_daily_loss_limit or None

        if capital <= 0:
            card.sizing_note = "Set TRADING_CAPITAL in .env for a size suggestion"
            return
        if lot <= 0 or per_unit_risk <= 0:
            return
        budget = capital * (self.cfg.risk_per_trade_pct / 100.0)
        per_lot_risk = per_unit_risk * lot
        lots = int(budget // per_lot_risk)
        card.suggested_lots = max(0, lots)
        card.sizing_note = (
            f"{lots} lot(s) risks ≈₹{lots * per_lot_risk:,.0f} "
            f"({self.cfg.risk_per_trade_pct:.1f}% of ₹{capital:,.0f}) if the stop is hit"
        )

    def evaluate_symbol(self, symbol: str, profile: ModeProfile) -> SignalResponse | None:
        engine = self.state.engine_for_symbol(symbol)
        if engine is None:
            return None
        now = int(time.time())

        # Session gate (round-2 audit): outside market hours the ticks/chain are
        # frozen leftovers — issuing a "fresh" card with a live countdown off
        # them is the most misleading thing an advisory tool can do. Reconcile a
        # market-closed no-trade instead (active cards still expire naturally).
        if not mcal.is_market_open():
            reason = mcal.market_closed_reason()
            status = MarketStatus(
                symbol=symbol, mode=profile.mode, regime=Regime.WARMING_UP,
                regime_label="Market Closed", bias=Bias.NEUTRAL,
                bull_score=0.0, bear_score=0.0, headline=reason,
                vix_status=None, news_label=None, news_net=None, notes=[reason],
            )
            closed = SignalResponse(
                symbol=symbol, mode=profile.mode, evaluated_at=now, status=status,
                action=Action.AVOID, signal=None, no_trade_reason=reason, score=None,
            )
            return self.store.reconcile(closed, now)
        df = engine.dataframe(profile.timeframe)
        # Evaluate on CLOSED candles only. The forming candle carries partial
        # volume (deflating volume_ratio ~10x early in a bucket) and flickering
        # patterns/structure; the chart still shows it via the REST candles API.
        secs = TIMEFRAME_SECONDS.get(profile.timeframe, 180)
        # `> 1`: never trim the ONLY candle away. Right after the 09:15 open (or
        # any day rollover) a single forming candle exists — trimming it yields
        # an empty frame that breaks downstream scoring. Keeping it is harmless
        # because the engine is still warming up (needs min_candles) and cannot
        # issue a signal from one bar.
        if len(df) > 1 and int(df["ts"].iloc[-1]) + secs > now:
            df = df.iloc[:-1]
        ind = compute_snapshot(df)
        snap = self.state.underlying_snapshot(symbol)
        chain = self.state.get_option_chain(chain_key(symbol, profile.expiry_key))
        vix = self.state.vix_snapshot()

        fresh = self.engine.evaluate(
            profile=profile,
            symbol=symbol,
            df=df,
            ind=ind,
            chain=chain,
            spot_ltp=snap.ltp if snap else None,
            fut_ltp=snap.fut_ltp if snap else None,
            prev_close=snap.prev_close if snap else None,
            day_high=snap.day_high if snap else None,
            day_low=snap.day_low if snap else None,
            vix_status=vix.status if vix else None,
            ticks=self.state.ticks,
            now=now,
            news=news_store.sentiment(symbol),
        )
        self._apply_sizing(fresh, symbol)
        return self.store.reconcile(fresh, now, self.throttle, self._risk_state(now))

    def evaluate_all(self) -> None:
        for symbol in self.cfg.signal_symbols:
            for profile in self.profiles.values():
                try:
                    self.evaluate_symbol(symbol, profile)
                except Exception as exc:  # pragma: no cover
                    log.warning("signal eval failed for %s/%s: %s", symbol, profile.mode.value, exc)
