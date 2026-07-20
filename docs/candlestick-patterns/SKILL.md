---
name: candlestick-patterns
description: Identify, explain, and apply Japanese candlestick patterns for technical analysis of stock, index, forex, crypto, or commodity price charts. Use this skill whenever the user asks about candlestick patterns, chart patterns, "what does this candle/setup mean", reversal/continuation/indecision signals, bullish or bearish formations (e.g. hammer, doji, engulfing, morning star, marubozu, three white soldiers), how to trade a pattern (entries, stops, confirmation), whether patterns actually work / how reliable they are, objective or coded identification rules, backtesting, or building a systematic candlestick strategy — even if they don't say the word "candlestick". Covers 40+ named patterns with structure, market psychology, signal strength, trading context, the empirical evidence, and implementation code.
---

# Candlestick Patterns

A reference for reading Japanese candlestick charts and interpreting the most common named patterns. Use it to identify a pattern the user describes, explain what a named pattern looks like and signals, teach candlestick analysis, or advise on how a pattern is typically traded.

Candlesticks work across any liquid market (equities, forex, commodities, indices, crypto) and any timeframe. Reliability is generally higher in liquid markets and on higher timeframes (daily/weekly), where each candle reflects more meaningful price action.

## Candle anatomy (read this first)

Every candlestick summarizes four prices over one period (a minute, hour, day, week): **open, high, low, close**.

- **Body** — the thick part, spanning open to close. A **bullish** (green/white) body means close > open; a **bearish** (red/black) body means close < open. A long body = strong conviction; a short body = indecision.
- **Wicks / shadows** — the thin lines to the session **high** (upper) and **low** (lower). A long upper wick shows sellers rejected higher prices; a long lower wick shows buyers rejected lower prices.
- **Doji** — open and close are nearly equal, so the body is a thin line (cross/plus shape). Signals indecision or possible trend exhaustion.
- **Marubozu** — a candle with a long body and essentially **no wicks**: price opened at one extreme and closed at the other. Shows total control by one side for the whole session (bullish marubozu = opens at low, closes at high; bearish = opens at high, closes at low). Often marks strong momentum or breakouts.

Reading a pattern is really reading the tug-of-war between buyers and sellers that produced these shapes.

> Quick fact: candlestick charting was developed by Japanese rice traders in the 1700s (Munehisa Homma is the figure most associated with it) and popularized for Western markets by Steve Nison's 1991 book.

## How to use patterns well

Patterns are **probabilistic, not deterministic** — they suggest a likely move, never guarantee it. State this clearly. Five rules to apply and to pass on to the user:

1. **Context is everything.** The same shape means different things in different places. A Hammer is a bullish reversal signal only *at the bottom of a downtrend*; the identical candle mid-range means little. Always establish the prior trend before calling a reversal.
2. **Wait for confirmation.** A single pattern is a hint, not a trigger — usually confirmed by the next candle closing in the signaled direction. Multi-candle patterns are already partly self-confirming, which is why they tend to be stronger.
3. **Combine with other tools.** Patterns forming *at* support/resistance, at a key moving average (e.g. the 200-day, or EMAs used as dynamic S/R), or when an oscillator (RSI, MACD, Stochastics) is overbought/oversold carry far more weight. Volume confirming the pattern (above-average volume on the signal candle) strengthens it.
4. **Weight by size and timeframe.** A pattern on a daily/weekly chart beats the same pattern on a 1-minute chart, and multi-candle patterns generally outweigh single-candle ones.
5. **Beware exhaustion.** Very large candles after an already-extended move can signal exhaustion rather than continuation (e.g. oversized Three White Soldiers/Black Crows).
6. **Be honest about the evidence.** The peer-reviewed record for candlesticks as standalone profit signals is mixed and generally weaker than their textbook reputation — apparent edges often shrink or vanish once transaction costs and rigorous statistics are applied. Present them as a descriptive language and a disciplined way to define entries/stops, not as reliable alpha. See `references/evidence-and-implementation.md` when reliability, backtesting, or "do these actually work" comes up.

Add a brief, honest caveat with any trading-relevant analysis: this is educational information about chart patterns, not financial advice, and patterns can and do fail.

## Trading a pattern (general framework)

When the user wants to act on a pattern, the standard approach is the same across most patterns, so apply this rather than reciting per-pattern rules:

- **Entry** — on the close of the confirming candle, or the open of the next session.
- **Stop-loss** — just beyond the pattern's extreme: below the low for bullish setups (e.g. below a hammer's low), above the high for bearish setups (e.g. above a shooting star's high).
- **Take-profit / target** — often the next support/resistance level; define it before entering so the risk-to-reward ratio is set in advance.
- **Confluence** — treat the signal as tradeable mainly when trend context, a key level, volume, and/or an indicator agree.

Decide the stop and target *before* opening the position, not after.

## The four families

- **Bullish reversal** — appear in a *downtrend*, signal a possible turn up.
- **Bearish reversal** — appear in an *uptrend*, signal a possible turn down.
- **Continuation** — signal the current trend resumes after a pause (bullish or bearish).
- **Neutral / indecision** — signal a balance between buyers and sellers; meaning depends heavily on context and what follows.

## Pattern index

Full structure, psychology, signal strength, and trade notes for each are in `references/pattern-catalog.md` — read that file when the user asks about a specific pattern, wants details or trade guidance, or when you need to match a description to a name. For reliability, objective/coded identification rules, backtesting, or systematic implementation, read `references/evidence-and-implementation.md` instead. Quick reference:

### Bullish (downtrend → up)
| Pattern | Candles | Type | Strength |
|---|---|---|---|
| Bullish Engulfing | 2 | Reversal | High |
| Bullish Marubozu | 1 | Reversal/continuation | Medium-High |
| Hammer | 1 | Reversal | Medium |
| Morning Star | 3 | Reversal | High |
| Piercing Line | 2 | Reversal | Medium |
| Bullish Harami | 2 | Reversal (early warning) | Low-Medium |
| Three White Soldiers | 3 | Reversal | High |
| Inverted Hammer | 1 | Reversal | Medium |
| Dragonfly Doji | 1 | Reversal | Medium |
| Bullish Abandoned Baby | 3 | Reversal | High (rare) |
| Three Inside Up | 3 | Reversal | Medium-High |
| Three Outside Up | 3 | Reversal | High |
| Bullish Kicker | 2 | Reversal | High |
| Tweezer Bottom | 2 | Reversal | Medium-High |
| Concealing Baby Swallow | 4 | Reversal | Medium (rare) |
| Bullish Belt Hold | 1 | Reversal | Medium |
| Ladder Bottom | 5 | Reversal | Medium |
| Meeting Lines (Bullish) | 2 | Reversal | Medium |
| Rising Three Methods | 5 | Continuation | Medium-High |
| Mat Hold | 5 | Continuation | Medium-High |
| Bullish Separating Lines | 2 | Continuation | Medium |
| Bullish Three-Line Strike | 4 | Continuation | Medium |

### Bearish (uptrend → down)
| Pattern | Candles | Type | Strength |
|---|---|---|---|
| Bearish Engulfing | 2 | Reversal | High |
| Bearish Marubozu | 1 | Reversal/continuation | Medium-High |
| Bearish Belt Hold | 1 | Reversal | Medium |
| Hanging Man | 1 | Reversal | Medium |
| Shooting Star | 1 | Reversal | Medium |
| Gravestone Doji | 1 | Reversal | Medium |
| Bearish Harami | 2 | Reversal (early warning) | Low-Medium |
| Bearish Doji Star | 2 | Reversal | Medium |
| Evening Star | 3 | Reversal | High |
| Bearish Abandoned Baby | 3 | Reversal | High (rare) |
| Upside Gap Two Crows | 3 | Reversal | Medium |
| Bearish Tweezer Top | 2 | Reversal | Medium-High |
| Bearish Kicker | 2 | Reversal | High |
| Three Inside Down | 3 | Reversal | Medium-High |
| Three Outside Down | 3 | Reversal | High |
| Dark Cloud Cover | 2 | Reversal | Medium |
| Three Black Crows | 3 | Reversal | High |
| Bearish Three-Line Strike | 4 | Continuation | Medium |
| Bearish Mat Hold | 5 | Continuation | Medium-High |
| Falling Three Methods | 5 | Continuation | Medium-High |

### Neutral / indecision (context decides)
| Pattern | Candles | Meaning |
|---|---|---|
| Doji | 1 | Indecision; exhaustion after a trend |
| Long-legged Doji | 1 | Extreme indecision (long wicks both sides) |
| Spinning Top | 1 | Indecision; loss of momentum in clusters |
| Inside Bar | 2 | Consolidation; breakout direction gives the signal |

(Dragonfly and Gravestone Doji are directional variants and are listed under Bullish/Bearish above.)

## Workflow for common requests

- **"What is the [X] pattern?"** → Open the catalog; give the structure (candle-by-candle), what it signals, the trend context it needs, and its typical signal strength.
- **"I'm seeing [description] on a chart — what is it?"** → Match the described candles (count, colors, bodies, wicks, gaps, prior trend) against the catalog. Name the closest pattern(s); for look-alikes (Hammer vs Dragonfly Doji vs Hanging Man; Shooting Star vs Gravestone Doji vs Inverted Hammer) explain the distinguishing feature and how context decides.
- **"Is this bullish or bearish?"** → State the family, tie it to the prior trend, and stress confirmation.
- **"How do I trade it?"** → Use the general trading framework above (entry on confirmation, stop beyond the pattern's extreme, target at the next level, confluence), specialized to the pattern.
- **"Do these actually work / how reliable is [X]?"** → Read `references/evidence-and-implementation.md` and give the honest, evidence-based answer: mixed academic support, edges often gone after costs, patterns best used with confluence or as model features.
- **"Give me objective/coded rules" / backtesting / systematic strategy** → Read `references/evidence-and-implementation.md` for the parameterized geometry (Body/Range/shadow math, ATR/rolling-median thresholds, 10-bar EMA trend filter), the backtest pitfalls (survivorship, data-snooping, costs/slippage), position sizing, and the rule-based pseudocode.
- **Teaching / overview** → Start with candle anatomy and the four families, then a few high-frequency patterns (Engulfing, Hammer/Shooting Star, Doji, Morning/Evening Star, Marubozu) rather than dumping the whole catalog.

When a visual would help the user grasp a pattern's shape, offer to sketch it (e.g. an inline diagram of the candle layout).
