"""Condor range regime + breakout risk — measured, persistent, and separate.

The directional engine's SIDEWAYS is a residual ("no directional edge"), which
is evidence of nothing. A condor needs the positive claim — "this tape is
range-bound and has been for a while" — so this classifier scores seven range
VOTES, applies hard volatility vetoes, and only grants RANGE_BOUND when the
vote has held across the last three closed 15m bars. It reuses the same seven
directional facts as signals/regime.py for its balance vote (V1) so the two
modules can never disagree about what "balanced" means, but it never touches
that module's code or labels.

All vote/veto thresholds are frozen here per the spec (pre-registered); the
module deliberately has no tuning knobs beyond what config exposes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd

from app.market.indicators import (
    atr,
    bollinger_width,
    compute_snapshot,
)
from app.models.schemas import IndicatorSnapshot
from app.signals.features import market_structure

# Labels
RANGE_BOUND = "RANGE_BOUND"
MILD_BULLISH = "MILD_BULLISH"
MILD_BEARISH = "MILD_BEARISH"
TRENDING = "TRENDING"
VOLATILE = "VOLATILE_UNSAFE"
WARMING_UP = "WARMING_UP"
NOT_QUALIFIED = "NOT_QUALIFIED"     # tape isn't disqualified — it just hasn't
                                    # earned the range label yet (persistence)

CONDOR_OK = {RANGE_BOUND, MILD_BULLISH, MILD_BEARISH}

_MIN_15M_BARS = 12
_PERSIST_BARS = 3      # bars required before a range label is granted
_MAX_PERSIST = 6       # bars measured — confidence saturates here (spec §3)


def _direction_vote(df: pd.DataFrame, ind: IndicatorSnapshot,
                    prev_close: float | None) -> int:
    """The 7-fact directional vote (net = bull - bear), mirroring
    signals/regime.py fact-for-fact so 'balanced' means the same thing in both
    engines. Returns net in [-7, +7]."""
    if df is None or df.empty:
        return 0
    price = float(df["close"].iloc[-1])
    bull = bear = 0
    if ind.vwap is not None:
        if price > ind.vwap:
            bull += 1
        else:
            bear += 1
    if ind.ema9 is not None and ind.ema20 is not None:
        if ind.ema9 > ind.ema20:
            bull += 1
        else:
            bear += 1
    if ind.ema20 is not None and ind.ema50 is not None:
        if ind.ema20 > ind.ema50:
            bull += 1
        else:
            bear += 1
    if ind.supertrend_dir == "up":
        bull += 1
    elif ind.supertrend_dir == "down":
        bear += 1
    st = market_structure(df)
    if st.label == "HH_HL":
        bull += 1
    elif st.label == "LH_LL":
        bear += 1
    if ind.rsi is not None:
        if ind.rsi > 55:
            bull += 1
        elif ind.rsi < 45:
            bear += 1
    if prev_close:
        if price > prev_close:
            bull += 1
        else:
            bear += 1
    return bull - bear


def _session_slice(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    last_day = (int(df["ts"].iloc[-1]) + 19800) // 86400
    day_start = last_day * 86400 - 19800
    return df[df["ts"] >= day_start]


def vwap_crossings(df5: pd.DataFrame, atr5: float | None) -> int:
    """Sign changes of (close - running session VWAP), ignoring bars hugging
    VWAP (|dist| < 0.1 ATR) so jitter doesn't count as two-way trade."""
    s = _session_slice(df5)
    if s is None or len(s) < 3:
        return 0
    tp = (s["high"] + s["low"] + s["close"]) / 3.0
    vol = s["volume"].fillna(0.0)
    cum_v = vol.cumsum().replace(0, pd.NA)
    vwap = (tp * vol).cumsum() / cum_v
    dist = (s["close"] - vwap).astype(float)
    if atr5 and atr5 > 0:
        dist = dist.where(dist.abs() >= 0.1 * atr5, other=pd.NA)
    signs = dist.dropna().apply(lambda x: 1 if x > 0 else -1)
    if signs.empty:
        return 0
    return int((signs != signs.shift()).sum()) - 1


@dataclass
class CondorRegime:
    label: str = WARMING_UP
    confidence: int = 0                     # 0-100, display only
    net15: int = 0
    adx15: float | None = None
    votes: dict = field(default_factory=dict)   # V1..V7 -> bool (latest bar)
    votes_sum: int = 0
    bars_held: int = 0                      # consecutive 15m bars label held
    vetoes: list = field(default_factory=list)
    range_low: float | None = None
    range_high: float | None = None
    range_sources: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def condor_allowed(self) -> bool:
        return self.label in CONDOR_OK


def _votes_for(df15: pd.DataFrame, df5: pd.DataFrame, prev_close: float | None,
               range_vs_typical: float | None) -> tuple[dict, int, float | None]:
    """The seven votes as of df15's last closed bar. df5 must already be
    sliced to that bar's close, so V4/V6/V7 are genuinely historical (review
    catch: freezing them at 'now' made 3-bar persistence into a 1-bar check
    wearing a persistence label). V5's baseline (typical range by this time
    of day) moves slowly and is evaluated at now — the one documented
    exception."""
    ind = compute_snapshot(df15)
    net = _direction_vote(df15, ind, prev_close)
    adx_now = ind.adx
    adx_series = None
    try:
        from app.market.indicators import adx as _adx
        adx_series = _adx(df15)
    except Exception:
        pass
    adx_prev = None
    if adx_series is not None and len(adx_series.dropna()) > 6:
        adx_prev = float(adx_series.dropna().iloc[-7])
    atr5_last = None
    try:
        a5 = atr(df5)
        if len(a5.dropna()):
            atr5_last = float(a5.dropna().iloc[-1])
    except Exception:
        pass
    crossings = vwap_crossings(df5, atr5_last)
    price = float(df15["close"].iloc[-1]) if len(df15) else None
    sess5 = _session_slice(df5)
    v7 = True
    if sess5 is not None and len(sess5) > 12:
        recent = sess5.tail(12)
        v7 = (float(recent["high"].max()) < float(sess5["high"].max())
              and float(recent["low"].min()) > float(sess5["low"].min()))
    ind5 = compute_snapshot(df5) if df5 is not None and len(df5) else None
    votes = {
        "V1_balanced": abs(net) <= 2,
        "V2_weak_trend": adx_now is not None and adx_now < 20,
        "V3_trend_not_building": (adx_now is not None and adx_prev is not None
                                  and adx_now <= adx_prev + 1),
        "V4_two_way": crossings >= 4,
        # Fail closed on a missing baseline: "range normal" is a positive
        # claim, and None is not evidence for it (review catch).
        "V5_range_normal": range_vs_typical is not None and range_vs_typical <= 110,
        "V6_near_value": (ind5 is not None and ind5.vwap is not None
                          and atr5_last is not None and price is not None
                          and abs(float(df5["close"].iloc[-1]) - ind5.vwap) <= 1.0 * atr5_last),
        "V7_extremes_respected": v7,
    }
    return votes, net, adx_now


def classify(df5: pd.DataFrame, df15: pd.DataFrame, prev_close: float | None,
             vix_status: str | None, vix_change_pct: float | None,
             range_vs_typical: float | None, gap_ended_at: float | None,
             oi_walls: tuple | None = None, levels: tuple | None = None,
             now_ts: float | None = None) -> CondorRegime:
    out = CondorRegime()
    now = time.time() if now_ts is None else now_ts
    if df15 is None or len(df15) < _MIN_15M_BARS or df5 is None or len(df5) < 12:
        out.notes.append("insufficient bars for regime")
        return out

    # ---- vetoes (any -> VOLATILE_UNSAFE) --------------------------------
    try:
        a5 = atr(df5).dropna()
        if len(a5) > 30 and float(a5.iloc[-1]) > 1.8 * float(a5.tail(30).median()):
            out.vetoes.append("ATR spike (>1.8x 30-bar median)")
    except Exception:
        pass
    if vix_status == "High":
        out.vetoes.append("VIX High")
    if vix_change_pct is not None and vix_change_pct >= 5.0:
        out.vetoes.append(f"VIX +{vix_change_pct:.1f}% intraday")
    if gap_ended_at and now - gap_ended_at < 600:
        out.vetoes.append("post-gap quiet period")
    try:
        bbw = bollinger_width(df5["close"]).dropna()
        if (len(bbw) > 40 and float(bbw.iloc[-1]) <= float(bbw.tail(40).quantile(0.20))
                and range_vs_typical is not None and range_vs_typical <= 60):
            out.vetoes.append("COILED: compressed bands + tiny range (expansion risk)")
    except Exception:
        pass

    # ---- votes on the last closed 15m bars (up to _MAX_PERSIST back) ----
    # df5 is sliced to each historical bar's close so every vote is what the
    # classifier WOULD have said at that bar, not a recolored present.
    held = 0
    votes_latest: dict = {}
    net_latest = 0
    adx_latest = None
    for back in range(_MAX_PERSIST):
        d15 = df15.iloc[: len(df15) - back] if back else df15
        if len(d15) < _MIN_15M_BARS:
            break
        bar_end = int(d15["ts"].iloc[-1]) + 900
        d5 = df5[df5["ts"] < bar_end]
        if len(d5) < 12:
            break
        votes, net, adx_now = _votes_for(d15, d5, prev_close, range_vs_typical)
        if back == 0:
            votes_latest, net_latest, adx_latest = votes, net, adx_now
        if sum(votes.values()) >= 5:
            held += 1
        else:
            break

    out.votes = votes_latest
    out.votes_sum = sum(votes_latest.values())
    out.net15 = net_latest
    out.adx15 = adx_latest
    out.bars_held = held

    if out.vetoes:
        out.label = VOLATILE
        return out

    if abs(net_latest) >= 5 or (adx_latest is not None and adx_latest >= 25):
        out.label = TRENDING
        out.notes.append(f"vote {net_latest:+d}, ADX {adx_latest:.0f}" if adx_latest else f"vote {net_latest:+d}")
        return out

    if held >= _PERSIST_BARS and out.votes_sum >= 5:
        out.label = RANGE_BOUND
    elif (2 < abs(net_latest) <= 4 and adx_latest is not None and adx_latest < 22
          and sum(v for k, v in votes_latest.items() if k != "V1_balanced") >= 4):
        # Mild drift: directional lean without trend mechanics. Persistence for
        # MILD reuses the same 3-bar rule on the non-V1 votes.
        mild_held = 0
        for back in range(_MAX_PERSIST):
            d15 = df15.iloc[: len(df15) - back] if back else df15
            if len(d15) < _MIN_15M_BARS:
                break
            bar_end = int(d15["ts"].iloc[-1]) + 900
            d5 = df5[df5["ts"] < bar_end]
            if len(d5) < 12:
                break
            votes, net, adxv = _votes_for(d15, d5, prev_close, range_vs_typical)
            if (sum(v for k, v in votes.items() if k != "V1_balanced") >= 4
                    and 2 < abs(net) <= 4 and adxv is not None and adxv < 22):
                mild_held += 1
            else:
                break
        if mild_held >= _PERSIST_BARS:
            out.label = MILD_BULLISH if net_latest > 0 else MILD_BEARISH
            out.bars_held = mild_held
        else:
            out.label = NOT_QUALIFIED
            out.notes.append("mild drift not yet persistent")
    else:
        out.label = NOT_QUALIFIED
        out.notes.append(f"range vote {out.votes_sum}/7 held {held}/{_PERSIST_BARS} bars")

    out.confidence = round(100.0 * (out.votes_sum / 7.0) * min(1.0, out.bars_held / 6.0))

    # ---- measured range boundaries --------------------------------------
    if oi_walls:
        support, resistance = oi_walls
        if support:
            out.range_low = support
            out.range_sources.append(f"max PE OI {support:.0f}")
        if resistance:
            out.range_high = resistance
            out.range_sources.append(f"max CE OI {resistance:.0f}")
    if levels:
        lvl_low, lvl_high = levels
        if lvl_low and (out.range_low is None or lvl_low > out.range_low):
            out.range_low = lvl_low
            out.range_sources.append(f"S/R level {lvl_low:.0f}")
        if lvl_high and (out.range_high is None or lvl_high < out.range_high):
            out.range_high = lvl_high
            out.range_sources.append(f"S/R level {lvl_high:.0f}")
    return out


# ---------------------------------------------------------------------------
# Breakout risk
# ---------------------------------------------------------------------------

@dataclass
class BreakoutRisk:
    score: int = 0
    band: str = "LOW"                # LOW / MEDIUM / HIGH / EXTREME
    parts: list = field(default_factory=list)


def breakout_risk(df5: pd.DataFrame, df15: pd.DataFrame,
                  vix_change_pct: float | None,
                  oi_shift_against: bool = False) -> BreakoutRisk:
    """Pre-registered point model (spec §9). Entries need <= MEDIUM."""
    b = BreakoutRisk()

    def add(pts: int, why: str) -> None:
        b.score += pts
        b.parts.append(f"+{pts} {why}")

    try:
        from app.market.indicators import adx as _adx
        s = _adx(df15).dropna()
        if len(s) > 7 and float(s.iloc[-1]) - float(s.iloc[-7]) >= 3:
            add(20, f"ADX15 rising ({s.iloc[-7]:.0f}->{s.iloc[-1]:.0f})")
    except Exception:
        pass
    try:
        a5 = atr(df5).dropna()
        if len(a5) > 30:
            med = float(a5.tail(30).median())
            cur = float(a5.iloc[-1])
            if med > 0 and cur > 1.8 * med:
                add(25, "ATR5 >1.8x median")
            elif med > 0 and cur > 1.5 * med:
                add(15, "ATR5 >1.5x median")
    except Exception:
        pass
    try:
        bbw = bollinger_width(df5["close"]).dropna()
        if len(bbw) > 20 and float(bbw.iloc[-1]) > 1.3 * float(bbw.tail(20).min()):
            add(10, "Bollinger width expanding")
    except Exception:
        pass
    try:
        ind5 = compute_snapshot(df5)
        a5 = atr(df5).dropna()
        if (ind5.vwap is not None and len(a5)
                and abs(float(df5["close"].iloc[-1]) - ind5.vwap) > 1.5 * float(a5.iloc[-1])):
            add(10, "price >1.5 ATR from VWAP")
    except Exception:
        pass
    try:
        sess = _session_slice(df5)
        if sess is not None and len(sess) > 20:
            vol_med = float(sess["volume"].tail(21).head(20).median())
            if vol_med > 0 and float(sess["volume"].iloc[-1]) >= 1.5 * vol_med:
                add(10, "volume spike >=1.5x median")
            recent = sess.tail(6)
            if (float(recent["high"].max()) >= float(sess["high"].max())
                    or float(recent["low"].min()) <= float(sess["low"].min())):
                add(15, "new session extreme in last 30 min")
    except Exception:
        pass
    if vix_change_pct is not None:
        if vix_change_pct >= 7.0:
            add(20, f"VIX +{vix_change_pct:.1f}%")
        elif vix_change_pct >= 4.0:
            add(10, f"VIX +{vix_change_pct:.1f}%")
    if oi_shift_against:
        add(10, "OI writing unwinding on dominant side")

    b.band = ("EXTREME" if b.score >= 65 else
              "HIGH" if b.score >= 45 else
              "MEDIUM" if b.score >= 25 else "LOW")
    return b
