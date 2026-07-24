"""Directional signal scoring (0-100).

Given a candidate direction (CE=bullish / PE=bearish), each weighted component
scores how strongly the current market supports that direction. Weights match
the product spec:

    price action 25 | trend 20 | volume 15 | options/OI 20 | volatility 10 | news 10

News is a Phase-4 placeholder (neutral 5/10). Volume returns half credit when
unavailable (index has no volume / market closed) so it neither rewards nor
unfairly zeroes the score.
"""
from __future__ import annotations

from app.models.schemas import IndicatorSnapshot
from app.news.models import NewsSentiment
from app.signals import patterns
from app.signals.features import OiAnalysis, market_structure, volume_ratio
from app.signals.models import Direction, ScoreBreakdown, ScoreComponent


def _price_action(df, ind: IndicatorSnapshot, bullish: bool, day_high, day_low) -> ScoreComponent:
    pts = 0.0
    reasons: list[str] = []
    # The engine scores both directions for the status display even while the
    # regime is WARMING_UP, so this can be called with no candles at all (first
    # bars after a session rollover). Never index into an empty frame.
    if df is None or len(df) == 0:
        return ScoreComponent(name="Price action & structure", points=0.0, max=25,
                              reasons=["No candles yet"])
    price = float(df["close"].iloc[-1])

    if ind.vwap is not None:
        if (price > ind.vwap) == bullish:
            pts += 8
            reasons.append("Price " + ("above" if bullish else "below") + " VWAP")
    struct = market_structure(df)
    want = "HH_HL" if bullish else "LH_LL"
    if struct.label == want:
        pts += 9
        reasons.append("Higher highs/lows" if bullish else "Lower highs/lows")
    elif struct.label == "MIXED":
        pts += 3

    # Breakout vs recent swing / day extreme
    ref_high = max(x for x in [struct.swing_high, day_high] if x is not None) if (struct.swing_high or day_high) else None
    ref_low = min(x for x in [struct.swing_low, day_low] if x is not None) if (struct.swing_low or day_low) else None
    if bullish and ref_high and price >= ref_high:
        pts += 8
        reasons.append("Breakout above resistance")
    elif not bullish and ref_low and price <= ref_low:
        pts += 8
        reasons.append("Breakdown below support")

    # Candlestick confirmation — a small, clamped nudge within the 25-pt budget
    # (patterns are a feature, not a driver; see signals/patterns.py).
    near = any(lvl and abs(price - lvl) / price <= 0.0015 for lvl in (ind.vwap, ind.ema20))
    pdelta, preasons = patterns.confirmation(df, bullish, near)
    pts += pdelta
    reasons.extend(preasons)
    pts = max(0.0, min(25.0, pts))

    return ScoreComponent(name="Price action & structure", points=round(pts, 1), max=25, reasons=reasons)


def _trend(ind: IndicatorSnapshot, bullish: bool) -> ScoreComponent:
    pts = 0.0
    reasons: list[str] = []
    if ind.ema9 is not None and ind.ema20 is not None and (ind.ema9 > ind.ema20) == bullish:
        pts += 7
        reasons.append("EMA9 " + (">" if bullish else "<") + " EMA20")
    if ind.ema20 is not None and ind.ema50 is not None and (ind.ema20 > ind.ema50) == bullish:
        pts += 5
        reasons.append("EMA20 " + (">" if bullish else "<") + " EMA50")
    if ind.supertrend_dir == ("up" if bullish else "down"):
        pts += 4
        reasons.append(f"Supertrend {ind.supertrend_dir}")
    if ind.rsi is not None:
        r = ind.rsi
        if bullish:
            if 55 < r <= 72:
                pts += 4; reasons.append(f"RSI {r:.0f} (bullish)")
            elif r > 72:
                pts += 1; reasons.append(f"RSI {r:.0f} (overbought)")
            elif 45 <= r <= 55:
                pts += 2
        else:
            if 28 <= r < 45:
                pts += 4; reasons.append(f"RSI {r:.0f} (bearish)")
            elif r < 28:
                pts += 1; reasons.append(f"RSI {r:.0f} (oversold)")
            elif 45 <= r <= 55:
                pts += 2
    return ScoreComponent(name="Trend & momentum", points=round(pts, 1), max=20, reasons=reasons)


def _volume(df, bullish: bool) -> ScoreComponent:
    vr = volume_ratio(df)
    if vr is None:
        return ScoreComponent(name="Volume confirmation", points=7.0, max=15,
                              reasons=["Volume unavailable (market closed / no vol)"])
    if vr >= 1.5:
        pts, note = 15.0, f"Volume {vr:.1f}x average"
    elif vr >= 1.2:
        pts, note = 10.0, f"Volume {vr:.1f}x average"
    elif vr >= 1.0:
        pts, note = 6.0, f"Volume {vr:.1f}x average"
    else:
        pts, note = 2.0, f"Volume light ({vr:.1f}x)"
    return ScoreComponent(name="Volume confirmation", points=pts, max=15, reasons=[note])


def _options(oi: OiAnalysis, bullish: bool, spot: float | None) -> ScoreComponent:
    pts = 0.0
    reasons: list[str] = []
    want = "bullish" if bullish else "bearish"
    if oi.bias == want:
        pts += 10
        reasons.extend(oi.notes[:1])
    elif oi.bias == "neutral":
        pts += 4
    # PCR alignment — but NOT when the bias itself came from PCR: that pays
    # one reading twice (up to +5 phantom points; the 22-Jul 09:15 card
    # carried "PCR 0.82" as both bias and alignment). Halved instead of
    # skipped when the bias was neutral-by-PCR-midzone: the reading still
    # carries some independent information about crowding.
    if oi.pcr is not None:
        double_counted = oi.bias_source == "pcr" and oi.bias == want
        if not double_counted:
            if bullish and oi.pcr >= 1.1:
                pts += 5; reasons.append(f"PCR {oi.pcr}")
            elif not bullish and oi.pcr <= 0.9:
                pts += 5; reasons.append(f"PCR {oi.pcr}")
            elif 0.9 < oi.pcr < 1.1:
                pts += 3
    # Wall context — direction-aware: reward only when the S/R walls are
    # positioned to favour the trade (support below + resistance overhead for a
    # CE; mirror for a PE), not merely for existing.
    if oi.support_strike is not None and oi.resistance_strike is not None and spot:
        if bullish:
            favourable = oi.support_strike <= spot < oi.resistance_strike
        else:
            favourable = oi.resistance_strike >= spot > oi.support_strike
        if favourable:
            pts += 5
            reasons.append(f"S/R walls {oi.support_strike:.0f}/{oi.resistance_strike:.0f} favour {'CE' if bullish else 'PE'}")
        else:
            pts += 1
    return ScoreComponent(name="Options & OI", points=round(pts, 1), max=20, reasons=reasons)


def _volatility(
    ind: IndicatorSnapshot, vix_status: str | None, vix_percentile: float | None = None
) -> ScoreComponent:
    mapping = {"Calm": 8, "Stable": 8, "Elevated": 5, "High": 2}
    pts = float(mapping.get(vix_status, 6))
    reasons = [f"VIX {vix_status}"] if vix_status else ["VIX n/a"]
    # Volatility PRICE, not just level. This engine only BUYS premium, and
    # premium bought at the rich end of VIX's own year loses to IV crush even
    # when the direction is right — the 23-Jul cards bought ~₹120 tops that an
    # absolute "Stable" reading happily blessed. Percentile is the IV-rank idea
    # applied at the index level until per-strike IV exists. None (history not
    # seeded) keeps the original level-only behaviour.
    if vix_percentile is not None:
        if vix_percentile >= 80:
            pts -= 3
            reasons.append(f"VIX pctl {vix_percentile:.0f} — premium rich vs its year")
        elif vix_percentile >= 60:
            pts -= 1
            reasons.append(f"VIX pctl {vix_percentile:.0f} — premium above median")
        elif vix_percentile <= 25:
            pts += 1
            reasons.append(f"VIX pctl {vix_percentile:.0f} — premium cheap vs its year")
        else:
            reasons.append(f"VIX pctl {vix_percentile:.0f}")
    if ind.atr is not None:
        pts += 2
    return ScoreComponent(name="Volatility condition",
                          points=max(0.0, min(pts, 10)), max=10, reasons=reasons)


def _news(sent: NewsSentiment | None, bullish: bool) -> ScoreComponent:
    if sent is None or sent.items_considered == 0:
        return ScoreComponent(name="News sentiment", points=5.0, max=10,
                              reasons=["No market-moving news"])
    if sent.label == "volatile":
        return ScoreComponent(name="News sentiment", points=3.0, max=10,
                              reasons=[f"Conflicting news — caution ({sent.items_considered} items)"])
    # +net supports a CE, −net supports a PE; scale into a 0-10 band around neutral 5.
    aligned = sent.net_score if bullish else -sent.net_score
    pts = max(0.0, min(10.0, 5.0 + (aligned / 100.0) * 5.0))
    verb = "supports" if aligned > 0 else "opposes" if aligned < 0 else "neutral for"
    return ScoreComponent(
        name="News sentiment", points=round(pts, 1), max=10,
        reasons=[f"News {sent.label} — {verb} {'CE' if bullish else 'PE'} (net {sent.net_score:+.0f}, {sent.items_considered} items)"],
    )


def score(
    direction: Direction,
    df,
    ind: IndicatorSnapshot,
    oi: OiAnalysis,
    vix_status: str | None = None,
    day_high: float | None = None,
    day_low: float | None = None,
    spot: float | None = None,
    news: NewsSentiment | None = None,
    vix_percentile: float | None = None,
) -> ScoreBreakdown:
    bullish = direction is Direction.CE
    components = [
        _price_action(df, ind, bullish, day_high, day_low),
        _trend(ind, bullish),
        _volume(df, bullish),
        _options(oi, bullish, spot),
        _volatility(ind, vix_status, vix_percentile),
        _news(news, bullish),
    ]
    total = round(sum(c.points for c in components), 1)
    return ScoreBreakdown(direction=direction, components=components, total=total, max=100)
