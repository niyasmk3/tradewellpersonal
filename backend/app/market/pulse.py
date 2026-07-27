"""Market pulse — what the tape is DOING right now, computed per request.

The score card says what the engine thinks of a setup; this says what the
market is doing while you read it: where price sits in the day's range, how
stretched it is from VWAP in ATR units, whether volume is running hot or thin
against recent sessions, how much of a typical day's range is already spent,
where put/call positioning has drifted since the session's first observation,
and what fear (VIX) is doing intraday.

Everything derives from state already in memory — the future's candle frames
(the multi-day 15m frame supplies the "typical day" baselines), the live
option chain, and the VIX tick. No new data sources, no persistence; every
field is None when its inputs aren't warm yet, and the UI says so instead of
inventing a number.
"""
from __future__ import annotations

import logging
import time

from app.market.indicators import compute_snapshot
from app.state import MarketState

log = logging.getLogger("tradewell.market")

_IST_OFFSET = 19800

# First PCR observed per (IST day, symbol). Captured on the first pulse
# computation of the day, so "shift" means "since Tradewell first looked
# today" — that is the open only when the dashboard was up at 09:15, and the
# UI labels it honestly as a session drift, not an official open print.
_pcr_first: dict[tuple[int, str], float] = {}


def _ist_day(ts: float) -> int:
    return int(ts + _IST_OFFSET) // 86400


def compute_pulse(state: MarketState, symbol: str) -> dict:
    symbol = symbol.upper()
    now = time.time()
    out: dict = {"symbol": symbol, "updated_at": int(now)}

    engine = state.engine_for_symbol(symbol)
    meta = state.underlyings.get(symbol)
    fut_ltp = None
    if meta and meta.fut_token:
        fut_ltp = state.ticks.get(meta.fut_token, {}).get("last_price")

    # --- day range + VWAP stretch, from the session (3m) frame --------------
    try:
        df = engine.dataframe("3m") if engine else None
        if df is not None and len(df) >= 3:
            day_high = float(df["high"].max())
            day_low = float(df["low"].min())
            last = fut_ltp or float(df["close"].iloc[-1])
            out["day_high"], out["day_low"], out["fut_ltp"] = day_high, day_low, last
            rng = day_high - day_low
            if rng > 0:
                out["range_pos_pct"] = round((last - day_low) / rng * 100, 1)
            ind = compute_snapshot(df)
            if ind.vwap and ind.atr:
                out["vwap"] = ind.vwap
                out["atr"] = ind.atr
                out["vwap_dist_atr"] = round((last - ind.vwap) / ind.atr, 2)
    except Exception:
        log.debug("pulse: session frame section failed", exc_info=True)

    # --- run-rates vs recent sessions, from the multi-day 15m frame ---------
    # Volume: mean per completed 15m bar today vs the same mean over prior
    # days in the frame. Range: today's high-low vs the mean prior daily range.
    # Both need at least one full prior session in the frame.
    try:
        df15 = engine.dataframe("15m") if engine else None
        if df15 is not None and len(df15) >= 10:
            days = df15["ts"].map(_ist_day)
            today = _ist_day(now)
            cur = df15[days == today]
            prior = df15[days < today]
            if len(cur) >= 2 and len(prior) >= 10:
                prior_days = prior.groupby(prior["ts"].map(_ist_day))
                # Drop the forming bar: its partial volume drags today's mean.
                cur_done = cur.iloc[:-1] if len(cur) > 2 else cur
                prior_vol = float(prior["volume"].mean())
                if prior_vol > 0:
                    out["vol_run_rate"] = round(float(cur_done["volume"].mean()) / prior_vol, 2)
                ranges = prior_days["high"].max() - prior_days["low"].min()
                typical = float(ranges.mean())
                if typical > 0 and "day_high" in out:
                    out["range_vs_typical_pct"] = round(
                        (out["day_high"] - out["day_low"]) / typical * 100, 1)
    except Exception:
        log.debug("pulse: 15m baseline section failed", exc_info=True)

    # --- put/call positioning drift -----------------------------------------
    try:
        chain = state.get_option_chain(f"{symbol}:nearest")
        if chain and chain.pcr is not None:
            out["pcr"] = chain.pcr
            key = (_ist_day(now), symbol)
            first = _pcr_first.setdefault(key, chain.pcr)
            out["pcr_first"] = first
            out["pcr_shift"] = round(chain.pcr - first, 2)
    except Exception:
        log.debug("pulse: pcr section failed", exc_info=True)

    # --- VIX intraday --------------------------------------------------------
    try:
        vix = state.ticks.get(state.vix_token, {}).get("last_price") if state.vix_token else None
        if vix:
            out["vix"] = vix
            closes = state.vix_daily_closes
            if closes:
                out["vix_chg_pct"] = round((vix - closes[-1]) / closes[-1] * 100, 2)
    except Exception:
        log.debug("pulse: vix section failed", exc_info=True)

    story = narrate(out)
    if story:
        out["story"] = story
    return out


def narrate(p: dict) -> str:
    """The pulse in plain language — deterministic, no model call.

    Fixed rules over the fields above, so the text can never say something the
    numbers don't, costs nothing at a 3-second cadence, and is unit-testable.
    Sentences are skipped when their inputs are missing; at most four are
    composed so it reads like a glance, not a report. Describes the MARKET,
    never recommends a trade — the score card above owns that judgement.
    """
    parts: list[str] = []

    pos, used = p.get("range_pos_pct"), p.get("range_vs_typical_pct")
    if pos is not None:
        where = ("near the top of today's range" if pos >= 70
                 else "near the bottom of today's range" if pos <= 30
                 else "in the middle of today's range")
        s = f"The market is trading {where}"
        if used is not None:
            if used >= 90:
                s += ", and it has already covered a full day's worth of movement — further big moves have to come from fresh energy"
            elif used >= 60:
                s += f", with a good chunk of a normal day's travel ({used:.0f}%) already done"
            else:
                s += ", and the day still has plenty of room to move"
        parts.append(s + ".")

    stretch = p.get("vwap_dist_atr")
    if stretch is not None:
        if abs(stretch) < 0.5:
            parts.append("Price is hugging its day-average (VWAP) — no real stretch either way.")
        elif abs(stretch) < 2:
            side = "above" if stretch > 0 else "below"
            who = "buyers" if stretch > 0 else "sellers"
            parts.append(f"Price is holding {side} the day's average price, so {who} have had the upper hand so far.")
        else:
            side = "above" if stretch > 0 else "below"
            parts.append(f"Price is stretched {abs(stretch):.1f} ATRs {side} its average — like the end of a rubber band, chasing from here is expensive.")

    rr = p.get("vol_run_rate")
    if rr is not None:
        if rr >= 1.3:
            parts.append(f"Trading activity is about {rr:.1f}x a normal session — these moves have real participation behind them.")
        elif rr <= 0.7:
            parts.append(f"Trading is thin ({rr:.1f}x normal) — moves can reverse easily without follow-through.")

    shift = p.get("pcr_shift")
    if shift is not None:
        if shift >= 0.05:
            parts.append("Option sellers have been building put positions under the market since the morning — usually a sign they expect it to hold up.")
        elif shift <= -0.05:
            parts.append("Put support has been unwinding since the morning — option sellers are less willing to stand under the market.")

    chg = p.get("vix_chg_pct")
    if chg is not None:
        if chg >= 3:
            parts.append(f"The fear index is up {chg:.1f}% today, inflating every option premium.")
        elif chg <= -3:
            parts.append(f"Fear is draining out (VIX {chg:.1f}%), so premiums are getting cheaper — easier to buy, quicker to decay.")

    return " ".join(parts[:4])
