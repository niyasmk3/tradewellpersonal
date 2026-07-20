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


def build_profiles(cfg: Settings) -> dict[str, ModeProfile]:
    """Return the enabled mode profiles keyed by mode value."""
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
            score_valid=72,                           # slightly stricter → fewer, higher-conviction
            score_wait=62,
            premium_sl_pct=0.30,                      # wider premium stop for multi-day swings
            rr_target1=2.0,
            rr_target2=3.5,
            strike_bias="itm_atm",                    # higher delta, less theta decay
            min_candles=16,
            horizon="Multi-day swing · monthly options",
        ),
    }
    return {m: all_profiles[m] for m in cfg.signal_mode_list if m in all_profiles}
