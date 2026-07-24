"""Trading-mode parameter profiles.

A "mode" is just a named parameter set fed into the same signal engine. Intraday
inherits the tuned Phase-2 values from settings; Positional/Swing is a distinct
profile: slower timeframe, monthly options, higher-delta strikes, wider stops,
bigger targets, longer entry-window validity.

NOTE: the positional profile is meaningful but *shallow* until historical daily
candles are added (Kite Historical Data add-on). On live-built candles alone,
higher-timeframe / multi-day trend context is thin, so it needs more warm-up and
its longer EMAs may be unavailable early in a session.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.signals.models import TradingMode


@dataclass(frozen=True)
class ModeProfile:
    mode: TradingMode
    label: str
    timeframe: str                # candle timeframe the engine evaluates
    expiry_key: str               # option-chain expiry bucket: "nearest" | "monthly"
    validity_seconds: int         # how long the ENTRY window stays fresh
    score_valid: int              # >= tradeable
    score_wait: int               # >= wait-for-confirmation
    premium_sl_pct: float
    rr_target1: float
    rr_target2: float
    strike_bias: str              # "atm_otm" (intraday) | "itm_atm" (positional)
    min_candles: int              # warm-up threshold
    horizon: str                  # human-readable holding horizon


def all_profiles(cfg: Settings) -> dict[str, ModeProfile]:
    """Every known profile, whether or not it is enabled in config.

    The journal needs a mode's stop/target geometry to re-price a trade against
    the actual fill, and a position can outlive the mode being switched off in
    `SIGNAL_MODES` — so that lookup must not depend on what is enabled today.
    """
    return _build_all(cfg)


def ladder_params(cfg: Settings, mode: TradingMode) -> tuple[float, float, float] | None:
    """(premium_sl_pct, rr_target1, rr_target2) for `mode`, or None if unknown.

    This is what lets `TradeStore.create_from_signal` rebuild the whole price
    ladder from your fill using the same numbers the card was built with.
    """
    p = _build_all(cfg).get(mode.value if isinstance(mode, TradingMode) else str(mode))
    return (p.premium_sl_pct, p.rr_target1, p.rr_target2) if p else None


def build_profiles(cfg: Settings) -> dict[str, ModeProfile]:
    """Return the enabled mode profiles keyed by mode value.

    Ordered by `SIGNAL_MODES`, not by declaration order: `evaluate_all` walks
    this dict, and the open-position cap is shared across modes — so whichever
    mode comes first takes the last free slot when both qualify in one cycle.
    """
    everything = _build_all(cfg)
    return {m: everything[m] for m in cfg.signal_mode_list if m in everything}


def _build_all(cfg: Settings) -> dict[str, ModeProfile]:
    all_profiles = {
        TradingMode.INTRADAY.value: ModeProfile(
            mode=TradingMode.INTRADAY,
            label="Intraday",
            timeframe=cfg.signal_timeframe,           # 3m
            expiry_key="nearest",                     # weekly for NIFTY
            validity_seconds=cfg.signal_validity_seconds,
            score_valid=cfg.score_valid,
            score_wait=cfg.score_wait,
            premium_sl_pct=cfg.premium_sl_pct,
            rr_target1=cfg.rr_target1,
            rr_target2=cfg.rr_target2,
            strike_bias="atm_otm",
            min_candles=15,
            horizon="Same-day · exit before close",
        ),
        TradingMode.POSITIONAL.value: ModeProfile(
            mode=TradingMode.POSITIONAL,
            label="Positional / Swing",
            timeframe="15m",
            expiry_key="monthly",
            validity_seconds=6 * 3600,                # entry window good for the session
            # NOTE: LOWER than intraday's 78, not stricter (an earlier comment
            # here claimed otherwise). Positional cards have issued at 72-77
            # with mixed results; the gate is under review with paper evidence
            # — raise it there, not by folklore.
            score_valid=72,
            score_wait=62,
            premium_sl_pct=0.30,                      # wider premium stop for multi-day swings
            rr_target1=2.0,
            rr_target2=3.5,
            strike_bias="itm_atm",                    # higher delta, less theta decay
            min_candles=16,
            horizon="Multi-day swing · monthly options",
        ),
    }
    return all_profiles
