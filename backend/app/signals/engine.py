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

        bull = scoring.score(Direction.CE, df, ind, oi, vix_status, dh, dl, spot=spot_ltp, news=news)
        bear = scoring.score(Direction.PE, df, ind, oi, vix_status, dh, dl, spot=spot_ltp, news=news)

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

        # Non-tradeable regimes short-circuit to no-trade / wait.
        if not reg.tradeable:
            wait_regimes = {Regime.COMPRESSION, Regime.BREAKOUT_DEVELOPING, Regime.REVERSAL, Regime.WARMING_UP}
            action = Action.WAIT if reg.regime in wait_regimes else Action.AVOID
            return resp(action, reason="; ".join(reg.notes) or reg.regime.value)

        direction = Direction.CE if reg.bias is Bias.BULLISH else Direction.PE
        chosen = bull if direction is Direction.CE else bear

        if chosen.total < profile.score_wait:
            return resp(Action.AVOID, reason=f"Score {chosen.total:.0f} below {profile.score_wait}", score=chosen)
        if chosen.total < profile.score_valid:
            return resp(Action.WAIT, reason=f"Setup forming — score {chosen.total:.0f} (needs {profile.score_valid})", score=chosen)

        # --- tradeable signal ---
        strong = reg.regime in (Regime.STRONG_BULLISH, Regime.STRONG_BEARISH) and chosen.total >= 80
        pick = strike_mod.select(
            symbol, direction, chain, spot_ltp or price_fut, strong,
            self.cfg.strike_min_oi, self.cfg.strike_max_spread_pct, ticks,
            strike_bias=profile.strike_bias,
        )
        if pick is None or pick.ltp is None:
            return resp(Action.AVOID, reason="No liquid strike available", score=chosen)

        plan = risk_mod.build(
            direction, pick.ltp, df, ind, price_fut, symbol, tf,
            profile.premium_sl_pct, profile.rr_target1, profile.rr_target2, basis,
            disaster_pct=self._disaster_pct(),
            quick_pct=self.cfg.quick_target_pct or None,
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
            ref_entry_premium=pick.ltp,
        )
        return resp(card.action, signal=card, score=chosen)
