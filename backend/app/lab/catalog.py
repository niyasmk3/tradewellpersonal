"""The 40-strategy catalog.

Each entry documents the hypothesis, the exact signal logic the implementation
follows (strategies/*.py must match this text — drift between the two is a
bug), the data the strategy genuinely needs, and a bounded parameter schema.

Data-need keys (resolved against real on-disk data by lab.data):
    index_5m    NIFTY 50 index 5-min bars (~3y, .patterns_candles.db)
    fut_candles NIFTY futures bars incl. volume (.lab_futures.db, Kite sync)
    fut_oi      NIFTY futures open interest (same sync, oi=1)
    chain_hist  option-chain snapshots (.condor_chain.db, 12-Aug-2026 →)
    vix_daily   India VIX daily closes

Honesty note: no strategy here may be backtested outside the span its data
actually covers. Option premiums are never modelled from the index. The
readiness layer, not this file, decides what is runnable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.lab.models import ParamSpec

INDEX_5M = "index_5m"
FUT_CANDLES = "fut_candles"
FUT_OI = "fut_oi"
CHAIN_HIST = "chain_hist"
VIX_DAILY = "vix_daily"

CAT_FUT = "futures"
CAT_OPT_BUY = "opt_buy"
CAT_OPT_SELL = "opt_sell"
CAT_CHAIN = "chain_oi"
CAT_VOL = "vol_expiry"
CAT_QUANT = "quant"

CATEGORY_LABELS = {
    CAT_FUT: "A · NIFTY Futures",
    CAT_OPT_BUY: "B · Options Buying",
    CAT_OPT_SELL: "C · Options Selling",
    CAT_CHAIN: "D · Chain & OI",
    CAT_VOL: "E · Volatility & Expiry",
    CAT_QUANT: "F · Quantitative",
}


@dataclass(frozen=True)
class StrategyDef:
    sid: str
    name: str
    category: str
    instrument: str            # "FUT" | "OPT" | "MULTI"
    spine: str                 # "fut" | "index"  (price series signals run on)
    timeframe: str
    hypothesis: str
    logic: str
    data_needs: tuple
    params: tuple
    overnight: bool = False


# --- shared parameter builders --------------------------------------------
# Minutes are IST minutes-of-day (09:15 = 555, 15:30 = 930), matching
# market/calendar.py and algo/contract.py conventions.

def _p_window(start: int = 560, end: int = 870, squareoff: int = 915) -> tuple:
    return (
        ParamSpec("entry_start_min", "Entry window start", "int", start, 555, 925,
                  1, unit="IST min", tunable=True,
                  help="No entries before this IST minute-of-day."),
        ParamSpec("entry_end_min", "Entry window end", "int", end, 556, 926, 1,
                  unit="IST min", tunable=True),
        ParamSpec("squareoff_min", "Square-off", "int", squareoff, 570, 929, 1,
                  unit="IST min",
                  help="Force-exit any open position at this IST minute."),
    )


def _p_trades(n: int = 2) -> ParamSpec:
    return ParamSpec("max_trades_per_day", "Max trades/day", "int", n, 1, 10, 1,
                     tunable=True)


def _p_lots(n: int = 1) -> ParamSpec:
    return ParamSpec("lots", "Lots", "int", n, 1, 20, 1,
                     help="Position size in lots (lot size from the live "
                          "instrument master / stored contract specs).")


def _p_bracket_atr(stop: float = 1.0, target: float = 1.5) -> tuple:
    return (
        ParamSpec("stop_atr", "Stop (ATR mult)", "float", stop, 0.3, 5.0, 0.1,
                  tunable=True),
        ParamSpec("target_atr", "Target (ATR mult)", "float", target, 0.3, 8.0,
                  0.1, tunable=True),
    )


def _p_bracket_prem(stop: float = 30.0, target: float = 50.0) -> tuple:
    return (
        ParamSpec("stop_pct", "Stop (% of premium)", "float", stop, 5, 90, 1,
                  unit="%", tunable=True),
        ParamSpec("target_pct", "Target (% of premium)", "float", target, 5,
                  300, 1, unit="%", tunable=True),
    )


def _p_trail() -> ParamSpec:
    return ParamSpec("trail_pct", "Trail (% off peak, 0=off)", "float", 0.0, 0,
                     80, 1, unit="%", tunable=True,
                     help="Once in profit, exit if value falls this % from "
                          "its peak. 0 disables.")


def _p_expiry() -> ParamSpec:
    return ParamSpec("expiry_rule", "Expiry", "choice", "weekly",
                     choices=("weekly", "monthly"))


def _p_strike_off(default: int = 0, lo: int = -3, hi: int = 6) -> ParamSpec:
    return ParamSpec("strike_offset", "Strike offset (steps OTM)", "int",
                     default, lo, hi, 1, tunable=True,
                     help="0 = ATM; +n = n strike-steps further OTM; "
                          "-n = ITM.")


def _p_liquidity() -> tuple:
    return (
        ParamSpec("min_oi", "Min OI", "int", 50000, 0, 10000000, 1000,
                  help="Skip contracts with less open interest."),
        ParamSpec("max_spread_pct", "Max bid-ask spread", "float", 1.5, 0.1,
                  10.0, 0.1, unit="%",
                  help="Skip fills when the quoted spread exceeds this."),
    )


def _p_vix_band() -> tuple:
    return (
        ParamSpec("vix_min", "Min VIX", "float", 0.0, 0, 60, 0.5, tunable=True),
        ParamSpec("vix_max", "Max VIX", "float", 99.0, 1, 99, 0.5, tunable=True),
    )


def _p_regime_filter() -> ParamSpec:
    return ParamSpec("regime_filter", "Regime filter", "choice", "off",
                     choices=("off", "trend_only", "range_only",
                              "exclude_highvol"), tunable=True)


_COMMON_FUT = _p_window() + (_p_trades(), _p_lots()) + _p_bracket_atr() + (
    _p_trail(), _p_regime_filter()) + _p_vix_band()
_COMMON_OPT = _p_window() + (_p_trades(), _p_lots(), _p_expiry()) + \
    _p_bracket_prem() + (_p_trail(),) + _p_liquidity() + (_p_regime_filter(),) \
    + _p_vix_band()


def _d(sid, name, category, instrument, spine, timeframe, hypothesis, logic,
       needs, extra=(), common=(), overnight=False) -> StrategyDef:
    return StrategyDef(
        sid=sid, name=name, category=category, instrument=instrument,
        spine=spine, timeframe=timeframe, hypothesis=hypothesis, logic=logic,
        data_needs=tuple(needs), params=tuple(extra) + tuple(common),
        overnight=overnight,
    )


# ==========================================================================
# CATEGORY A — NIFTY FUTURES
# ==========================================================================

_FUT_DEFS = (
    _d("F01", "Opening Range Breakout", CAT_FUT, "FUT", "fut", "5m",
       "The first minutes of the session set a reference range; a decisive "
       "break of it tends to continue as trapped openers unwind.",
       "Range = high/low of the first `orb_minutes`. Enter on the first bar "
       "CLOSING beyond the range inside the entry window; stop at the "
       "opposite range edge (capped at stop_atr*ATR), target = target_atr*"
       "ATR. One attempt per side per day.",
       (FUT_CANDLES,),
       extra=(ParamSpec("orb_minutes", "Opening range", "int", 15, 5, 60, 5,
                        unit="min", tunable=True),),
       common=_COMMON_FUT),

    _d("F02", "EMA 9/21 Crossover", CAT_FUT, "FUT", "fut", "5m",
       "Short-horizon trend persistence: when the fast EMA crosses the slow "
       "one with trend strength behind it, drift continues.",
       "Long when EMA(fast) crosses above EMA(slow) with ADX >= adx_min; "
       "short on the inverse. Exit on the reverse cross or the bracket.",
       (FUT_CANDLES,),
       extra=(ParamSpec("ema_fast", "Fast EMA", "int", 9, 3, 50, 1, tunable=True),
              ParamSpec("ema_slow", "Slow EMA", "int", 21, 5, 200, 1, tunable=True),
              ParamSpec("adx_min", "Min ADX", "float", 18, 0, 50, 1, tunable=True)),
       common=_COMMON_FUT),

    _d("F03", "Supertrend + ADX", CAT_FUT, "FUT", "fut", "5m",
       "Supertrend flips lag but filter chop well when demanded together "
       "with a rising ADX.",
       "Enter on a Supertrend direction flip with ADX >= adx_min. Exit on "
       "the opposite flip or the bracket.",
       (FUT_CANDLES,),
       extra=(ParamSpec("st_period", "ST period", "int", 10, 5, 30, 1, tunable=True),
              ParamSpec("st_mult", "ST multiplier", "float", 3.0, 1.0, 6.0, 0.5,
                        tunable=True),
              ParamSpec("adx_min", "Min ADX", "float", 20, 0, 50, 1, tunable=True)),
       common=_COMMON_FUT),

    _d("F04", "VWAP Trend Following", CAT_FUT, "FUT", "fut", "5m",
       "Session VWAP is the day's fair value; sustained trade on one side "
       "of it marks directional control.",
       "Enter long after `hold_bars` consecutive closes above VWAP with "
       "EMA20 rising (mirror for short). Exit on a close across VWAP or "
       "the bracket.",
       (FUT_CANDLES,),
       extra=(ParamSpec("hold_bars", "Confirm bars", "int", 3, 1, 10, 1,
                        tunable=True),),
       common=_COMMON_FUT),

    _d("F05", "Prev-Day High/Low Breakout", CAT_FUT, "FUT", "fut", "5m",
       "Yesterday's extremes are widely watched liquidity levels; genuine "
       "breaks draw follow-through.",
       "Enter on the first close beyond the previous session's high (long) "
       "or low (short), once per level per day. Stop stop_atr*ATR behind "
       "the level.",
       (FUT_CANDLES,),
       common=_COMMON_FUT),

    _d("F06", "Gap Momentum", CAT_FUT, "FUT", "fut", "5m",
       "Large opening gaps that do not immediately fill tend to extend in "
       "the gap direction (patterns module: big-gap no-fill ~23%).",
       "If |open - prev close| >= gap_min_pct, wait `confirm_bars`; enter "
       "in the gap direction only if price has not retraced past the "
       "midpoint of the gap.",
       (FUT_CANDLES,),
       extra=(ParamSpec("gap_min_pct", "Min gap", "float", 0.35, 0.1, 2.0,
                        0.05, unit="%", tunable=True),
              ParamSpec("confirm_bars", "Confirm bars", "int", 2, 1, 6, 1,
                        tunable=True)),
       common=_COMMON_FUT),

    _d("F07", "Gap-Fill Reversal", CAT_FUT, "FUT", "fut", "5m",
       "Moderate gaps into no news often mean-revert to the prior close "
       "as the auction finds real interest.",
       "If gap between gap_min_pct and gap_max_pct and the first "
       "`confirm_bars` fail to extend the gap, enter toward the previous "
       "close; target = the fill (capped by target_atr*ATR).",
       (FUT_CANDLES,),
       extra=(ParamSpec("gap_min_pct", "Min gap", "float", 0.2, 0.05, 1.0,
                        0.05, unit="%", tunable=True),
              ParamSpec("gap_max_pct", "Max gap", "float", 0.6, 0.1, 2.0,
                        0.05, unit="%", tunable=True),
              ParamSpec("confirm_bars", "Confirm bars", "int", 3, 1, 6, 1,
                        tunable=True)),
       common=_COMMON_FUT),

    _d("F08", "RSI + VWAP Mean Reversion", CAT_FUT, "FUT", "fut", "5m",
       "Intraday stretches from VWAP with washed-out momentum snap back "
       "toward fair value.",
       "Long when RSI < rsi_low AND price < VWAP - dist_atr*ATR; short on "
       "the mirror. Target = VWAP; stop = stop_atr*ATR beyond entry.",
       (FUT_CANDLES,),
       extra=(ParamSpec("rsi_low", "RSI oversold", "float", 30, 10, 45, 1,
                        tunable=True),
              ParamSpec("rsi_high", "RSI overbought", "float", 70, 55, 90, 1,
                        tunable=True),
              ParamSpec("dist_atr", "Min VWAP distance", "float", 1.2, 0.3,
                        4.0, 0.1, unit="ATR", tunable=True)),
       common=_COMMON_FUT),

    _d("F09", "Bollinger Squeeze Breakout", CAT_FUT, "FUT", "fut", "5m",
       "Volatility contraction precedes expansion; a band break out of a "
       "squeeze rides the new expansion.",
       "Squeeze = BB width below its trailing `squeeze_pctile` percentile "
       "(window squeeze_lookback). Enter on the first close outside a band "
       "while squeezed.",
       (FUT_CANDLES,),
       extra=(ParamSpec("squeeze_pctile", "Squeeze percentile", "float", 25,
                        5, 50, 5, unit="%", tunable=True),
              ParamSpec("squeeze_lookback", "Squeeze lookback", "int", 60, 20,
                        300, 10, unit="bars", tunable=True)),
       common=_COMMON_FUT),

    _d("F10", "Futures Price + OI", CAT_FUT, "FUT", "fut", "5m",
       "Rising OI with rising price = long buildup (fresh longs funding a "
       "move); rising OI with falling price = short buildup.",
       "Over `look_bars`: price change >= px_atr*ATR AND OI change >= "
       "oi_min_pct% in the confirming direction -> enter with the move.",
       (FUT_CANDLES, FUT_OI),
       extra=(ParamSpec("look_bars", "Lookback", "int", 6, 2, 24, 1,
                        tunable=True),
              ParamSpec("px_atr", "Min price move", "float", 0.8, 0.2, 3.0,
                        0.1, unit="ATR", tunable=True),
              ParamSpec("oi_min_pct", "Min OI change", "float", 1.0, 0.1,
                        10.0, 0.1, unit="%", tunable=True)),
       common=_COMMON_FUT),
)

# ==========================================================================
# CATEGORY B — OPTIONS BUYING (signals on the index spine, fills from the
# real chain archive; premium history exists only inside the chain window)
# ==========================================================================

_OPT_BUY_DEFS = (
    _d("B01", "ATM Momentum Buying", CAT_OPT_BUY, "OPT", "index", "5m",
       "Sharp index momentum bursts overwhelm theta on ATM options for a "
       "short holding window.",
       "|return over `look_bars`| >= burst_atr*ATR -> buy ATM CE (up) / PE "
       "(down). Premium bracket + trail; square-off by time.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("look_bars", "Burst lookback", "int", 3, 1, 12, 1,
                        tunable=True),
              ParamSpec("burst_atr", "Min burst", "float", 1.2, 0.4, 4.0, 0.1,
                        unit="ATR", tunable=True)),
       common=_COMMON_OPT),

    _d("B02", "ORB Option Buying", CAT_OPT_BUY, "OPT", "index", "5m",
       "Opening-range breaks expressed through options cap risk at premium "
       "while keeping breakout upside.",
       "F01's ORB signal on the index; on trigger buy CE/PE at "
       "strike_offset. Premium bracket.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("orb_minutes", "Opening range", "int", 15, 5, 60, 5,
                        unit="min", tunable=True),
              _p_strike_off(0)),
       common=_COMMON_OPT),

    _d("B03", "Supertrend CE/PE", CAT_OPT_BUY, "OPT", "index", "5m",
       "Supertrend flips on the index, expressed in long options to bound "
       "risk through whipsaws.",
       "Supertrend flip with ADX >= adx_min -> buy CE on bullish flip, PE "
       "on bearish. Exit on reverse flip or premium bracket.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("st_period", "ST period", "int", 10, 5, 30, 1, tunable=True),
              ParamSpec("st_mult", "ST multiplier", "float", 3.0, 1.0, 6.0,
                        0.5, tunable=True),
              ParamSpec("adx_min", "Min ADX", "float", 20, 0, 50, 1, tunable=True),
              _p_strike_off(0)),
       common=_COMMON_OPT),

    _d("B04", "VWAP Breakout Buying", CAT_OPT_BUY, "OPT", "index", "5m",
       "A clean reclaim of VWAP with participation marks intraday control "
       "changing hands.",
       "Close crosses VWAP with the bar range >= range_atr*ATR -> buy the "
       "directional option after `confirm_bars` holds.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("confirm_bars", "Confirm bars", "int", 2, 1, 6, 1,
                        tunable=True),
              ParamSpec("range_atr", "Min bar range", "float", 0.8, 0.2, 3.0,
                        0.1, unit="ATR", tunable=True),
              _p_strike_off(0)),
       common=_COMMON_OPT),

    _d("B05", "Delta-Based Strike Selection", CAT_OPT_BUY, "OPT", "index", "5m",
       "For a given directional view, the delta chosen — not the direction "
       "— decides the payoff shape; targeting a fixed delta standardises it.",
       "B01's momentum trigger, but the strike is chosen by BS delta "
       "closest to delta_target (from stored chain IV), not by moneyness.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("look_bars", "Burst lookback", "int", 3, 1, 12, 1,
                        tunable=True),
              ParamSpec("burst_atr", "Min burst", "float", 1.2, 0.4, 4.0, 0.1,
                        unit="ATR", tunable=True),
              ParamSpec("delta_target", "Target |delta|", "float", 0.40, 0.10,
                        0.60, 0.05, tunable=True)),
       common=_COMMON_OPT),

    _d("B06", "RSI Reversal Buying", CAT_OPT_BUY, "OPT", "index", "5m",
       "Momentum exhaustion at RSI extremes precedes snap-backs worth more "
       "than the theta spent waiting.",
       "RSI crosses back up through rsi_low -> buy CE; crosses back down "
       "through rsi_high -> buy PE.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("rsi_low", "RSI oversold", "float", 30, 10, 45, 1,
                        tunable=True),
              ParamSpec("rsi_high", "RSI overbought", "float", 70, 55, 90, 1,
                        tunable=True),
              _p_strike_off(0)),
       common=_COMMON_OPT),

    _d("B07", "Breakout + Volume Confirmation", CAT_OPT_BUY, "OPT", "index", "5m",
       "Breakouts of the day's range on unusual participation are less "
       "likely to be stop-runs.",
       "Close beyond the day's prior high/low AND volume-proxy z-score >= "
       "vol_z (vs same-time-of-day baseline) -> buy directional option.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("vol_z", "Min volume z", "float", 1.5, 0.5, 4.0, 0.1,
                        tunable=True),
              _p_strike_off(0)),
       common=_COMMON_OPT),

    _d("B08", "Expiry-Day Directional", CAT_OPT_BUY, "OPT", "index", "5m",
       "On expiry day gamma is enormous: a small index push multiplies "
       "near-ATM premiums, if entered after the morning shakeout.",
       "Expiry days only, after start_min: VWAP side + Supertrend agree -> "
       "buy strike_offset OTM in that direction. Hard time stop.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("start_min", "Earliest entry", "int", 660, 570, 900,
                        5, unit="IST min", tunable=True),
              _p_strike_off(1)),
       common=_COMMON_OPT),
)

# ==========================================================================
# CATEGORY C — OPTIONS SELLING (multi-leg; margin figures are estimates)
# ==========================================================================

_OPT_SELL_DEFS = (
    _d("S01", "ATM Short Straddle", CAT_OPT_SELL, "MULTI", "index", "5m",
       "Realised intraday movement is usually smaller than the straddle "
       "prices in; theta accrues to the seller on quiet days.",
       "Sell ATM CE + ATM PE at entry time. Stop on combined premium rising "
       "stop_pct%; exit at squareoff. Optional per-leg stop.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("entry_min", "Entry time", "int", 585, 560, 840, 5,
                        unit="IST min", tunable=True),
              ParamSpec("leg_stop_pct", "Per-leg stop (0=off)", "float", 0.0,
                        0, 300, 5, unit="%", tunable=True)),
       common=_COMMON_OPT),

    _d("S02", "OTM Short Strangle", CAT_OPT_SELL, "MULTI", "index", "5m",
       "Wings sold outside the expected move decay to nothing on most "
       "sessions, with more room than a straddle.",
       "Sell CE and PE `strike_offset` steps OTM at entry time; same stop/"
       "exit discipline as S01.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("entry_min", "Entry time", "int", 585, 560, 840, 5,
                        unit="IST min", tunable=True),
              _p_strike_off(3, 1, 10)),
       common=_COMMON_OPT),

    _d("S03", "Dynamic Delta-Neutral Straddle", CAT_OPT_SELL, "MULTI",
       "index", "5m",
       "A straddle re-centred when spot drifts keeps collecting theta "
       "while capping directional bleed.",
       "S01 entry; when spot drifts >= recenter_steps strike-steps from "
       "the sold strike, close both legs and re-sell ATM (max max_rolls "
       "rolls/day). Combined-premium stop applies to the live pair.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("entry_min", "Entry time", "int", 585, 560, 840, 5,
                        unit="IST min", tunable=True),
              ParamSpec("recenter_steps", "Re-centre after (steps)", "int", 2,
                        1, 6, 1, tunable=True),
              ParamSpec("max_rolls", "Max rolls/day", "int", 2, 0, 6, 1,
                        tunable=True)),
       common=_COMMON_OPT),

    _d("S04", "Iron Condor", CAT_OPT_SELL, "MULTI", "index", "5m",
       "Defined-risk premium selling: short strangle financed against "
       "bought wings caps tail risk at a known figure.",
       "Sell CE/PE short_offset steps OTM, buy wings wing_width further "
       "out, at entry time. Stop at credit_stop_pct% of net credit lost; "
       "exit at squareoff.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("entry_min", "Entry time", "int", 585, 560, 840, 5,
                        unit="IST min", tunable=True),
              ParamSpec("short_offset", "Short strikes (steps OTM)", "int", 3,
                        1, 10, 1, tunable=True),
              ParamSpec("wing_width", "Wing width (steps)", "int", 3, 1, 10,
                        1, tunable=True),
              ParamSpec("credit_stop_pct", "Stop (% of credit)", "float", 100,
                        20, 300, 10, unit="%", tunable=True)),
       common=_COMMON_OPT),

    _d("S05", "Iron Butterfly", CAT_OPT_SELL, "MULTI", "index", "5m",
       "Maximum theta at the money with bought wings: profits when the "
       "index pins near the entry strike.",
       "Sell ATM CE + PE, buy wings wing_width steps out, at entry time. "
       "Credit-based stop as S04.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("entry_min", "Entry time", "int", 585, 560, 840, 5,
                        unit="IST min", tunable=True),
              ParamSpec("wing_width", "Wing width (steps)", "int", 4, 1, 12,
                        1, tunable=True),
              ParamSpec("credit_stop_pct", "Stop (% of credit)", "float", 100,
                        20, 300, 10, unit="%", tunable=True)),
       common=_COMMON_OPT),

    _d("S06", "Bull Put Credit Spread", CAT_OPT_SELL, "MULTI", "index", "5m",
       "In an uptrend, OTM puts are systematically over-bought insurance; "
       "selling them with a bought wing defines the risk.",
       "Trend filter (EMA20 > EMA50 and price > VWAP) -> sell PE "
       "short_offset steps OTM, buy PE wing_width further out.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("short_offset", "Short strike (steps OTM)", "int", 3,
                        1, 10, 1, tunable=True),
              ParamSpec("wing_width", "Wing width (steps)", "int", 3, 1, 10,
                        1, tunable=True),
              ParamSpec("credit_stop_pct", "Stop (% of credit)", "float", 100,
                        20, 300, 10, unit="%", tunable=True)),
       common=_COMMON_OPT),

    _d("S07", "Bear Call Credit Spread", CAT_OPT_SELL, "MULTI", "index", "5m",
       "Mirror of S06 for downtrends: rallies into resistance sell calls "
       "with defined risk.",
       "Trend filter (EMA20 < EMA50 and price < VWAP) -> sell CE "
       "short_offset steps OTM, buy CE wing_width further out.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("short_offset", "Short strike (steps OTM)", "int", 3,
                        1, 10, 1, tunable=True),
              ParamSpec("wing_width", "Wing width (steps)", "int", 3, 1, 10,
                        1, tunable=True),
              ParamSpec("credit_stop_pct", "Stop (% of credit)", "float", 100,
                        20, 300, 10, unit="%", tunable=True)),
       common=_COMMON_OPT),

    _d("S08", "IV-Rank Premium Selling", CAT_OPT_SELL, "MULTI", "index", "5m",
       "Premium selling pays when implied vol is rich relative to its own "
       "recent history, not on every day.",
       "Compute ATM IV percentile over the stored chain history "
       "(iv_lookback days). Enter S02's strangle only when percentile >= "
       "iv_rank_min.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("entry_min", "Entry time", "int", 585, 560, 840, 5,
                        unit="IST min", tunable=True),
              ParamSpec("iv_rank_min", "Min IV percentile", "float", 60, 10,
                        95, 5, unit="%", tunable=True),
              ParamSpec("iv_lookback", "IV lookback", "int", 20, 5, 60, 1,
                        unit="days", tunable=True),
              _p_strike_off(3, 1, 10)),
       common=_COMMON_OPT),
)

# ==========================================================================
# CATEGORY D — OPTION CHAIN & OPEN INTEREST
# ==========================================================================

_CHAIN_DEFS = (
    _d("O01", "Put-Call Ratio Extremes", CAT_CHAIN, "OPT", "index", "5m",
       "PCR at extremes marks one-sided positioning; unwinds move the "
       "index against the crowd.",
       "OI-based PCR from the chain window: >= pcr_high -> contrarian CE "
       "buy; <= pcr_low -> contrarian PE buy, after `confirm_bars` of "
       "price agreement.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("pcr_high", "PCR high", "float", 1.4, 1.0, 3.0, 0.05,
                        tunable=True),
              ParamSpec("pcr_low", "PCR low", "float", 0.7, 0.2, 1.0, 0.05,
                        tunable=True),
              ParamSpec("confirm_bars", "Confirm bars", "int", 2, 1, 6, 1,
                        tunable=True)),
       common=_COMMON_OPT),

    _d("O02", "OI Buildup Breakout", CAT_CHAIN, "OPT", "index", "5m",
       "Fast OI accumulation near ATM alongside a price push signals "
       "informed positioning for a continuation.",
       "Net near-ATM dOI skew (CE vs PE writing, over oi_window snapshots) "
       "beyond oi_skew_min, aligned with price momentum -> buy with the "
       "move.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("oi_window", "dOI window", "int", 6, 2, 24, 1,
                        unit="snaps", tunable=True),
              ParamSpec("oi_skew_min", "Min dOI skew", "float", 0.15, 0.02,
                        0.8, 0.01, tunable=True)),
       common=_COMMON_OPT),

    _d("O03", "OI Support/Resistance", CAT_CHAIN, "OPT", "index", "5m",
       "The max-put-OI strike acts as support and max-call-OI as "
       "resistance while writers defend them.",
       "Price within touch_atr*ATR of the max-PE-OI wall and holding -> "
       "long CE; mirror at the max-CE-OI wall -> long PE. Invalidate on a "
       "close through the wall.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("touch_atr", "Touch distance", "float", 0.5, 0.1,
                        2.0, 0.1, unit="ATR", tunable=True),),
       common=_COMMON_OPT),

    _d("O04", "Change-in-OI Momentum", CAT_CHAIN, "OPT", "index", "5m",
       "The pace of OI change, not its level, carries the freshest "
       "positioning information.",
       "Rolling dOI momentum (PE-side minus CE-side, near ATM) crossing "
       "mom_min with price confirming -> directional option buy.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("mom_window", "Momentum window", "int", 6, 2, 24, 1,
                        unit="snaps", tunable=True),
              ParamSpec("mom_min", "Min momentum", "float", 0.1, 0.01, 1.0,
                        0.01, tunable=True)),
       common=_COMMON_OPT),

    _d("O05", "Straddle Premium Range Break", CAT_CHAIN, "OPT", "index", "5m",
       "The ATM straddle price is the market's live bet on movement; its "
       "own breakout front-runs an index move.",
       "Track combined ATM straddle premium. Break above its trailing "
       "`range_snaps` high by break_pct% -> buy straddle (movement "
       "regime); decay below range low -> optional short-straddle mode.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("range_snaps", "Range window", "int", 24, 6, 96, 2,
                        unit="snaps", tunable=True),
              ParamSpec("break_pct", "Break threshold", "float", 5.0, 1.0,
                        25.0, 0.5, unit="%", tunable=True),
              ParamSpec("mode", "Mode", "choice", "buy_break",
                        choices=("buy_break", "sell_decay", "both"),
                        tunable=True)),
       common=_COMMON_OPT),

    _d("O06", "Chain Composite Reversal", CAT_CHAIN, "OPT", "index", "5m",
       "When price sits at an OI wall with stretched PCR and exhausted "
       "momentum, the path of least resistance is back inside the range.",
       "Composite: price at an OI wall + PCR beyond its band + RSI extreme "
       "-> buy the reversal option. All three must agree.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("pcr_high", "PCR high", "float", 1.3, 1.0, 3.0, 0.05,
                        tunable=True),
              ParamSpec("pcr_low", "PCR low", "float", 0.75, 0.2, 1.0, 0.05,
                        tunable=True),
              ParamSpec("rsi_low", "RSI oversold", "float", 30, 10, 45, 1,
                        tunable=True),
              ParamSpec("rsi_high", "RSI overbought", "float", 70, 55, 90, 1,
                        tunable=True)),
       common=_COMMON_OPT),
)

# ==========================================================================
# CATEGORY E — VOLATILITY & EXPIRY
# ==========================================================================

_VOL_DEFS = (
    _d("V01", "VIX Regime Strategy", CAT_VOL, "MULTI", "index", "5m",
       "The VIX percentile tells which side of the vol trade is cheap: "
       "sell rich vol in quiet-range days, own cheap vol into squeezes.",
       "VIX percentile >= sell_pctile AND range-bound regime -> S02-style "
       "strangle. VIX percentile <= buy_pctile AND squeeze -> long ATM "
       "straddle.",
       (INDEX_5M, CHAIN_HIST, VIX_DAILY),
       extra=(ParamSpec("sell_pctile", "Sell-vol percentile", "float", 75, 50,
                        99, 1, tunable=True),
              ParamSpec("buy_pctile", "Buy-vol percentile", "float", 25, 1,
                        50, 1, tunable=True),
              _p_strike_off(3, 1, 10)),
       common=_COMMON_OPT),

    _d("V02", "Implied vs Realised Vol", CAT_VOL, "MULTI", "index", "5m",
       "The IV-RV spread is the option seller's gross margin; trade "
       "premium only when the spread is abnormal.",
       "RV = annualised stdev of index 5m returns over rv_days. ATM IV - "
       "RV >= spread_min -> sell straddle; <= -spread_min -> buy straddle.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("rv_days", "RV lookback", "int", 5, 2, 20, 1,
                        unit="days", tunable=True),
              ParamSpec("spread_min", "Min IV-RV spread", "float", 3.0, 0.5,
                        15.0, 0.5, unit="vol pts", tunable=True)),
       common=_COMMON_OPT),

    _d("V03", "Calendar Spread", CAT_VOL, "MULTI", "index", "5m",
       "Weekly options decay faster than monthlies at the same strike; "
       "short the fast leg against the slow one when the ratio is rich.",
       "At entry time, if weekly-ATM premium / monthly-ATM premium >= "
       "ratio_min: sell weekly ATM straddle legs, buy monthly same-strike "
       "legs. Held to weekly-expiry morning or credit stop (overnight).",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("entry_min", "Entry time", "int", 600, 560, 840, 5,
                        unit="IST min", tunable=True),
              ParamSpec("ratio_min", "Min weekly/monthly ratio", "float",
                        0.45, 0.2, 0.9, 0.01, tunable=True),
              ParamSpec("hold_days_max", "Max hold", "int", 2, 1, 4, 1,
                        unit="days", tunable=True)),
       common=_COMMON_OPT, overnight=True),

    _d("V04", "Expiry-Day Premium Decay", CAT_VOL, "MULTI", "index", "5m",
       "On expiry day, remaining extrinsic value burns to zero by close; "
       "selling it with a stop harvests the burn (closing-module lesson: "
       "theta beats direction after 15:00).",
       "Expiry days only: sell strangle strike_offset steps OTM at "
       "entry_min; combined-premium stop; exit at squareoff.",
       (INDEX_5M, CHAIN_HIST),
       extra=(ParamSpec("entry_min", "Entry time", "int", 615, 560, 870, 5,
                        unit="IST min", tunable=True),
              _p_strike_off(2, 0, 8)),
       common=_COMMON_OPT),

    _d("V05", "Delta-Hedged Gamma Scalping", CAT_VOL, "MULTI", "index", "5m",
       "A hedged long straddle earns |realised movement| against theta "
       "paid; profitable exactly when RV beats IV.",
       "Buy ATM straddle at entry time; when net BS delta drifts beyond "
       "hedge_delta, hedge with futures lots to neutral (max max_hedges "
       "adjustments). Exit at squareoff or premium bracket.",
       (INDEX_5M, CHAIN_HIST, FUT_CANDLES),
       extra=(ParamSpec("entry_min", "Entry time", "int", 585, 560, 840, 5,
                        unit="IST min", tunable=True),
              ParamSpec("hedge_delta", "Hedge trigger |delta|", "float", 0.20,
                        0.05, 0.60, 0.05, tunable=True),
              ParamSpec("max_hedges", "Max hedges/day", "int", 6, 0, 20, 1,
                        tunable=True)),
       common=_COMMON_OPT),
)

# ==========================================================================
# CATEGORY F — ADVANCED QUANT
# ==========================================================================

_QUANT_DEFS = (
    _d("Q01", "Intraday Regime Rotation", CAT_QUANT, "OPT", "index", "5m",
       "No single edge works in every tape; routing to trend-following in "
       "trends and fading in ranges beats either alone.",
       "Lab regime engine per bar: trending -> B03-style directional buy "
       "with the trend; range-bound -> fade day-range extremes; uncertain "
       "/ high-vol -> stand aside.",
       (INDEX_5M, CHAIN_HIST, VIX_DAILY),
       extra=(ParamSpec("fade_edge_pct", "Range-fade edge", "float", 80, 60,
                        95, 1, unit="% of day range", tunable=True),),
       common=_COMMON_OPT),

    _d("Q02", "ML Directional (Logistic)", CAT_QUANT, "FUT", "fut", "5m",
       "A small, transparent linear model on causal features can hold a "
       "modest edge if validated strictly walk-forward — and exposes "
       "exactly what it learned.",
       "Numpy logistic regression on causal features (lagged returns, RSI, "
       "VWAP distance, ATR percentile, time-of-day, gap). Trained on a "
       "trailing train_days window, refit each refit_days; trade when "
       "P(up) leaves [0.5-band, 0.5+band]. Never trained on data it is "
       "scored on.",
       (FUT_CANDLES, INDEX_5M),
       extra=(ParamSpec("train_days", "Training window", "int", 60, 20, 250,
                        5, unit="days", tunable=True),
              ParamSpec("refit_days", "Refit every", "int", 5, 1, 20, 1,
                        unit="days", tunable=True),
              ParamSpec("band", "Signal band", "float", 0.08, 0.02, 0.25,
                        0.01, tunable=True),
              ParamSpec("horizon_bars", "Label horizon", "int", 12, 3, 36, 1,
                        unit="bars", tunable=True)),
       common=_COMMON_FUT),

    _d("Q03", "Multi-Signal Ensemble", CAT_QUANT, "FUT", "fut", "5m",
       "Independent mediocre signals agree mostly when right; demanding "
       "agreement trades less and better.",
       "Five primitives (EMA cross state, Supertrend dir, VWAP side, RSI "
       "zone, prev-day-level break) vote each bar; enter when >= min_votes "
       "agree on a side, exit when agreement drops below exit_votes.",
       (FUT_CANDLES,),
       extra=(ParamSpec("min_votes", "Votes to enter", "int", 4, 2, 5, 1,
                        tunable=True),
              ParamSpec("exit_votes", "Votes to stay", "int", 2, 1, 5, 1,
                        tunable=True)),
       common=_COMMON_FUT),
)


CATALOG: tuple = (_FUT_DEFS + _OPT_BUY_DEFS + _OPT_SELL_DEFS + _CHAIN_DEFS
                  + _VOL_DEFS + _QUANT_DEFS)

BY_SID = {d.sid: d for d in CATALOG}

assert len(CATALOG) == 40, "catalog must hold exactly 40 strategies"
assert len(BY_SID) == 40, "duplicate strategy ids"


def get(sid: str) -> StrategyDef:
    try:
        return BY_SID[sid]
    except KeyError:
        raise KeyError("unknown strategy id: %s" % sid)


def as_dicts() -> list:
    """JSON-ready catalog for the API."""
    out = []
    for d in CATALOG:
        out.append({
            "sid": d.sid,
            "name": d.name,
            "category": d.category,
            "category_label": CATEGORY_LABELS[d.category],
            "instrument": d.instrument,
            "spine": d.spine,
            "timeframe": d.timeframe,
            "hypothesis": d.hypothesis,
            "logic": d.logic,
            "data_needs": list(d.data_needs),
            "overnight": d.overnight,
            "params": [
                {
                    "key": p.key, "label": p.label, "kind": p.kind,
                    "default": p.default, "lo": p.lo, "hi": p.hi,
                    "step": p.step, "choices": list(p.choices),
                    "tunable": p.tunable, "unit": p.unit, "help": p.help,
                }
                for p in d.params
            ],
        })
    return out
