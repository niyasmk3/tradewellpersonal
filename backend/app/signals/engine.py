"""Signal engine — orchestrates regime → score → strike → risk → SignalCard.

`evaluate` is intentionally stateless and takes explicit inputs (no globals) so
it is fully unit-testable with synthetic data. The stabilising SignalStore
(store.py) wraps it to keep an active signal steady across evaluations.
"""
from __future__ import annotations

from app.config import Settings
from app.models.schemas import IndicatorSnapshot, OptionChain
from app.news.models import NewsSentiment
from app.signals import regime as regime_mod
from app.signals import risk as risk_mod
from app.signals import scoring
from app.signals import strike as strike_mod
from app.signals.features import oi_analysis
from app.signals.modes import ModeProfile
from app.signals.models import (
    Action,
    Bias,
    Direction,
    MarketStatus,
    Regime,
    SignalCard,
    SignalResponse,
    SignalState,
)

def premium_quote(
    ticks: dict | None, token: int | None, chain_ltp: float, now: int
) -> tuple[float, int | None]:
    """The freshest premium available for `token`, and that quote's age.

    Returns (price, age_seconds). Two sources exist for an option's premium and
    they fail independently: the CHAIN snapshot (chain_ltp) is rebuilt on a
    poll and can freeze while ticks still flow; the TICK stream can die for one
    token while the chain keeps returning its last build. Preferring the tick's
    last_price whenever one exists, but always aging the quote by the tick's
    exchange timestamp, covers both: a fresh tick corrects a stale chain, and a
    dead tick stream turns into a large age the caller can refuse.

    age None means "no timestamped tick at all" — the caller cannot distinguish
    fresh from ancient, which for issuing a priced card is the same as stale.
    (The `ts` is the exchange's own stamp, so a pre-open tick carried over from
    yesterday ages by a full day rather than looking current.)
    """
    tick = (ticks or {}).get(token) or {}
    ltp = tick.get("last_price")
    price = float(ltp) if ltp and ltp > 0 else chain_ltp
    ts = tick.get("ts")
    return price, (max(0, now - int(ts)) if ts else None)


_HEADLINES = {
    Regime.STRONG_BULLISH: "Search for CE opportunity",
    Regime.MODERATE_BULLISH: "Search for CE opportunity",
    Regime.STRONG_BEARISH: "Search for PE opportunity",
    Regime.MODERATE_BEARISH: "Search for PE opportunity",
    Regime.SIDEWAYS: "No trade — sideways",
    Regime.COMPRESSION: "Wait for a breakout",
    Regime.BREAKOUT_DEVELOPING: "Breakout forming — wait for confirmation",
    Regime.NEWS_VOLATILITY: "Volatile — trade with caution",
    Regime.REVERSAL: "Possible reversal — wait",
    Regime.UNSAFE: "Stand aside — unstable market",
    Regime.WARMING_UP: "Warming up",
}


class SignalEngine:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg

    def _disaster_pct(self) -> float | None:
        """The backstop percentage, or None to keep the premium stop governing.

        GATED ON CONFIGURED CAPITAL. The whole justification for tolerating a
        45% stop is that sizing divides by it and hands back proportionally
        fewer lots. With TRADING_CAPITAL unset that division never runs
        (_apply_sizing returns early), so the wider stop would be a pure
        increase in rupees at risk with nothing holding size down. Opt in by
        setting TRADING_CAPITAL and RISK_PER_TRADE_PCT.
        """
        if self.cfg.stop_primary != "underlying" or self.cfg.trading_capital <= 0:
            return None
        return self.cfg.premium_disaster_pct

    def evaluate(
        self,
        *,
        profile: ModeProfile,
        symbol: str,
        df,
        ind: IndicatorSnapshot,
        chain: OptionChain | None,
        spot_ltp: float | None,
        fut_ltp: float | None,
        prev_close: float | None,
        day_high: float | None,
        day_low: float | None,
        vix_status: str | None,
        ticks: dict | None,
        now: int,
        news: NewsSentiment | None = None,
        vix_percentile: float | None = None,
        rr1_override: float | None = None,
    ) -> SignalResponse:
        tf = profile.timeframe
        price_fut = float(df["close"].iloc[-1]) if df is not None and len(df) else (fut_ltp or spot_ltp or 0.0)
        basis = (fut_ltp - spot_ltp) if (fut_ltp and spot_ltp) else 0.0

        # Convert index-space levels into futures space for consistent comparisons.
        dh = (day_high + basis) if day_high is not None else None
        dl = (day_low + basis) if day_low is not None else None
        pc = (prev_close + basis) if prev_close is not None else None

        oi = oi_analysis(chain, spot_ltp)
        reg = regime_mod.classify(df, ind, prev_close=pc, vix_status=vix_status, min_candles=profile.min_candles)

        bull = scoring.score(Direction.CE, df, ind, oi, vix_status, dh, dl,
                             spot=spot_ltp, news=news, vix_percentile=vix_percentile)
        bear = scoring.score(Direction.PE, df, ind, oi, vix_status, dh, dl,
                             spot=spot_ltp, news=news, vix_percentile=vix_percentile)

        status = MarketStatus(
            symbol=symbol,
            mode=profile.mode,
            regime=reg.regime,
            regime_label=regime_mod.regime_label(reg.regime),
            bias=reg.bias,
            bull_score=bull.total,
            bear_score=bear.total,
            headline=_HEADLINES.get(reg.regime, ""),
            vix_status=vix_status,
            news_label=news.label if news and news.items_considered else None,
            news_net=news.net_score if news and news.items_considered else None,
            notes=reg.notes,
        )

        def resp(action: Action, signal=None, reason=None, score=None) -> SignalResponse:
            return SignalResponse(
                symbol=symbol, mode=profile.mode, evaluated_at=now, status=status,
                action=action, signal=signal, no_trade_reason=reason, score=score,
            )

        # Non-tradeable regimes short-circuit to no-trade / wait. The BEST
        # direction's breakdown still rides along: the score panel (and its
        # trend history) must not go blank just because the regime gates the
        # trade — "compression, bear building toward 78" is exactly the
        # context worth watching.
        if not reg.tradeable:
            wait_regimes = {Regime.COMPRESSION, Regime.BREAKOUT_DEVELOPING, Regime.REVERSAL, Regime.WARMING_UP}
            action = Action.WAIT if reg.regime in wait_regimes else Action.AVOID
            best = bull if bull.total >= bear.total else bear
            return resp(action, reason="; ".join(reg.notes) or reg.regime.value, score=best)

        direction = Direction.CE if reg.bias is Bias.BULLISH else Direction.PE
        chosen = bull if direction is Direction.CE else bear

        if chosen.total < profile.score_wait:
            return resp(Action.AVOID, reason=f"Score {chosen.total:.0f} below {profile.score_wait}", score=chosen)
        if chosen.total < profile.score_valid:
            return resp(Action.WAIT, reason=f"Setup forming — score {chosen.total:.0f} (needs {profile.score_valid})", score=chosen)

        # MOVE EXHAUSTION: a fresh quote on a spent move is still a chase.
        # The future stretched several ATRs beyond EMA20 is momentum that has
        # already happened — trend confirmation there buys the tail (every
        # 23-Jul card fired at one).
        max_ext = self.cfg.signal_max_extension_atr
        if (
            max_ext > 0 and ind.atr and ind.ema20 and price_fut
            and abs(price_fut - ind.ema20) / ind.atr > max_ext
        ):
            ext = abs(price_fut - ind.ema20) / ind.atr
            return resp(
                Action.WAIT, score=chosen,
                reason=(f"Tape extended {ext:.1f} ATR from EMA20 (limit {max_ext:g}) — "
                        "move looks spent, wait for a pullback or consolidation"),
            )

        # --- tradeable signal ---
        strong = reg.regime in (Regime.STRONG_BULLISH, Regime.STRONG_BEARISH) and chosen.total >= 80
        pick = strike_mod.select(
            symbol, direction, chain, spot_ltp or price_fut, strong,
            self.cfg.strike_min_oi, self.cfg.strike_max_spread_pct, ticks,
            strike_bias=profile.strike_bias,
        )
        if pick is None or pick.ltp is None:
            return resp(Action.AVOID, reason="No liquid strike available", score=chosen)

        # FRESHNESS GATE + freshest-price override. The chain snapshot the pick
        # came from can lag the tape by minutes (candle-verified on 21/22-Jul:
        # 5 of 9 cards were priced on stale premiums, one at the PREVIOUS DAY'S
        # CLOSE while the contract opened 10% higher — every zone/SL/target on
        # those cards described a market that no longer existed). The rule:
        # price the plan from the newest quote available, and if even that is
        # older than the cutoff, say so and issue nothing.
        entry_px, quote_age = premium_quote(ticks, pick.token, pick.ltp, now)
        max_age = self.cfg.signal_max_premium_age_s
        if max_age > 0 and ticks is not None:
            if quote_age is None:
                return resp(
                    Action.AVOID, score=chosen,
                    reason=(f"No timestamped quote for {pick.tradingsymbol or pick.strike} "
                            "— refusing to price a card on an unverifiable premium"),
                )
            if quote_age > max_age:
                return resp(
                    Action.AVOID, score=chosen,
                    reason=(f"Premium quote for {pick.tradingsymbol or pick.strike} is "
                            f"{quote_age}s old (limit {max_age}s) — the zone it would "
                            "price no longer exists"),
                )

        plan = risk_mod.build(
            direction, entry_px, df, ind, price_fut, symbol, tf,
            profile.premium_sl_pct, rr1_override or profile.rr_target1,
            profile.rr_target2, basis,
            disaster_pct=self._disaster_pct(),
            quick_pct=self.cfg.quick_target_pct or None,
        )

        # INVALIDATION ROOM: the structural stop must be a real distance away.
        # A card whose invalidation sits inside one ATR of spot is priced to
        # die on noise — 23-Jul's first card had 0.4 points of room against a
        # 19-point ATR and its paper twin lasted 3 minutes.
        min_room_atr = self.cfg.signal_min_invalidation_atr
        if (
            min_room_atr > 0 and ind.atr and spot_ltp
            and plan.invalidation_level is not None
        ):
            room = abs(spot_ltp - plan.invalidation_level)
            if room < ind.atr * min_room_atr:
                return resp(
                    Action.WAIT, score=chosen,
                    reason=(f"Invalidation only {room:.0f} pts away (< {min_room_atr:g} "
                            f"ATR of {ind.atr:.0f}) — no room for the thesis to breathe"),
                )

        reasons: list[str] = []
        for c in chosen.components:
            reasons.extend(c.reasons)
        reasons.extend(pick.rationale[:1])
        reasons = [r for r in reasons if r][:7]

        bullish = direction is Direction.CE
        kind = ("BULLISH BREAKOUT" if strong else "BULLISH SETUP") if bullish else ("BEARISH BREAKDOWN" if strong else "BEARISH SETUP")

        card = SignalCard(
            id=f"{symbol}-{profile.mode.value}-{now}-{direction.value}",
            symbol=symbol,
            mode=profile.mode,
            title=f"{symbol} {kind}",
            action=Action.BUY_CE if bullish else Action.BUY_PE,
            direction=direction,
            state=SignalState.ACTIVE,
            contract=pick.tradingsymbol or f"{symbol} {int(pick.strike)} {direction.value}",
            strike=pick.strike,
            token=pick.token,
            expiry=chain.expiry if chain else None,
            entry_low=plan.entry_low,
            entry_high=plan.entry_high,
            premium_sl=plan.premium_sl,
            disaster_sl=plan.disaster_sl,
            quick_target=plan.quick_target,
            target1=plan.target1,
            target2=plan.target2,
            trailing_sl_rule=plan.trailing_sl_rule,
            risk_reward=plan.risk_reward,
            confidence=chosen.total,
            underlying_invalidation=plan.underlying_invalidation,
            invalidation_note=plan.invalidation_note,
            invalidation_level=plan.invalidation_level,
            invalidation_dir=plan.invalidation_dir,
            reasons=reasons,
            created_at=now,
            valid_until=now + profile.validity_seconds,
            score=chosen,
            ref_spot=spot_ltp,
            ref_entry_premium=entry_px,
        )
        return resp(card.action, signal=card, score=chosen)
