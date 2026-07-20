# Evidence, Objective Identification & Systematic Implementation

Read this file when the user asks whether candlestick patterns actually work, wants objective/coded identification rules (not just verbal descriptions), asks about backtesting, transaction costs, or building a systematic/quant strategy, or wants an honest assessment of a pattern's reliability. It complements `pattern-catalog.md` (which covers what patterns look like and mean) with the quantitative and empirical side.

The one-line takeaway to convey: candlestick patterns are a useful *descriptive language* for price action and a disciplined way to define entries and stops, but the peer-reviewed evidence for them as standalone profit signals is mixed and generally weaker than their textbook reputation — apparent edges often shrink or vanish once transaction costs and rigorous statistics are applied.

## Contents
- [Objective identification (parameterize the geometry)](#objective-identification)
- [What the empirical evidence says](#what-the-empirical-evidence-says)
- [Why backtests overstate reality](#why-backtests-overstate-reality)
- [Building a systematic candlestick strategy](#building-a-systematic-candlestick-strategy)
- [Use by trading style](#use-by-trading-style)

---

## Objective identification

Verbal words like "long" and "short" body are not implementable. There is no single universal definition for every pattern name, so any serious system must make the geometry explicit and *relative*. For each bar define:

- **Body** = |Close − Open|
- **Range** = High − Low
- **Upper shadow** = High − max(Open, Close)
- **Lower shadow** = min(Open, Close) − Low

Then classify "long" / "short" against a rolling reference rather than absolute numbers — e.g. compare Body and shadows to their rolling median or to ATR over the last ~20 bars. This keeps the same rule working across instruments and volatility regimes.

**Trend context must be coded too.** Reversal patterns are only valid after a prior trend, so gate them with a trend filter. A common, literature-backed default (following Morris and used in Marshall et al.) is a 10-bar exponential moving average: require price below the EMA for bullish-reversal candidates, above it for bearish ones.

Concrete example thresholds (tune to the market):

- **Hammer** (needs downtrend): `lower_shadow ≥ 2 × body` and `upper_shadow ≤ 0.25 × body` and the body sits in the top ~30% of the range.
- **Shooting Star** (needs uptrend): `upper_shadow ≥ 2 × body` and `lower_shadow ≤ 0.25 × body` and the body sits in the bottom ~30% of the range.
- **Bullish Engulfing** (needs downtrend): prior candle bearish, current bullish, `Open ≤ prior Close` and `Close ≥ prior Open` (current real body engulfs the prior real body).

Note that even standardized libraries (e.g. TA-Lib's `CDLHAMMER`, `CDLENGULFING`, `CDLMORNINGSTAR`) don't always verify classical context such as a prior trend — so add that filter yourself rather than assuming the detector handles it.

## What the empirical evidence says

The academic record is genuinely mixed, and the honest framing is "conditional, market-specific, often insignificant after costs" — not "these work" and not "these are useless." Present it that way.

- **Caginalp & Laurent (1998), US S&P 500 stocks, daily** — an early influential study that *found* out-of-sample significance and a small (~1%) two-day profit. Often cited as the supportive baseline.
- **Marshall, Young & Rose (2006), US DJIA stocks, daily** — a landmark *skeptical* result: after bootstrap testing, no statistically significant profitability. Hit rates hovered near 50% (e.g. Hammer ~49.6% positive, Bullish Engulfing ~49.1%).
- **Marshall, Young & Cahan (2008), large Japanese equities, daily** — no positive abnormal returns, even before costs.
- **Fock, Klein & Zwergel (2005), futures, intraday** — classical patterns not profitable; intraday costs were material.
- **Duvinage, Mazza & Petitjean (2013), DJIA stocks, 5-minute** — some rules beat buy-and-hold *gross*, but no candlestick rule or system outperformed *after* trading costs.
- **Lu (2014) and Lu, Chen & Hsu (2015), Taiwan / US, daily** — some patterns profitable after costs, and crucially, **exit/holding logic mattered more than the trend definition**: a liquidation-style exit could be profitable where a fixed-horizon exit was not; shorter (~3-day) holds often beat longer (~10-day) holds.
- **Tharavanij et al. (2017), Thailand SET50, daily** — most patterns not useful; several "significant" returns carried high risk or even flipped the textbook direction.
- **Orquín-Serrano et al. (2020), EURUSD forex, 30m–daily** — some pre-cost predictive power at lower timeframes (win rates ~53–59%), but **every selected strategy went negative after a one-pip round-trip cost**. Statistical edge present, economic edge absent — the canonical illustration of why raw hit-rate isn't enough.
- **Ho et al. (2021), top-23 cryptocurrencies, daily** — 68 common patterns were of little use; more low-accuracy patterns than useful ones.
- **Recent reinforcement-learning work (2020s)** — candlestick shapes used as *features* inside a model (not standalone rules) produced better risk-adjusted results (e.g. Sharpe ~1.6, lower drawdown) than a pure rule-based candlestick baseline. Suggests candles are more useful as inputs to broader models than as discretionary triggers.

Three durable conclusions from the synthesis:
1. Pattern **names** are stable; identification **thresholds** are not — make them explicit.
2. **Entry/exit logic matters as much as pattern selection.** The same pattern in the same market can look profitable or not depending purely on the holding/exit rule.
3. **Costs and slippage are make-or-break**, especially intraday. A gross edge smaller than a tick/pip/spread is probably not tradeable.

## Why backtests overstate reality

Flag these whenever someone shares or plans a candlestick backtest:

- **Survivorship bias** — testing on *today's* index constituents or top coins projected backward overweights winners and ignores delisted/failed names. Use historical membership with proper delisting treatment.
- **Data-snooping / multiple testing** — trying many patterns, parameters, and exits inflates the odds of a spurious "winner." Rigorous studies correct for this (Bonferroni, SSPA/reality-check, Monte-Carlo null distributions); casual backtests usually don't, which is why their edges evaporate on validation.
- **Transaction costs & slippage** — model both fixed costs (commission, spread) and stochastic adverse fills. Slippage-cancels-out assumptions may hold in deep majors under calm conditions but are unsafe in small caps, thin futures, volatile crypto, or around events.
- **Timeframe & session robustness** — gap-dependent textbook variants (stars, piercing/dark-cloud, gap patterns) are cleaner in session-based equity markets. In 24-hour forex and 24/7 crypto, redefine "gap," "penetration," and "star" to fit that microstructure rather than applying the literal equity definition.

## Building a systematic candlestick strategy

A good system is not "find pattern → trade." Specify, in order: **universe & session → bar construction → volatility & trend filter → pattern geometry → confirmation → entry → stop → exit → position size → walk-forward validation with fees and slippage.** Decide stop, target, and size *before* the backtest, not after.

**Risk-based position sizing** (use this, not fixed shares/contracts):

```text
risk_budget   = account_equity * risk_fraction
stop_distance = abs(entry_price - stop_price) + estimated_slippage + per_unit_cost
position_size = floor(risk_budget / stop_distance)
```

Lower the `risk_fraction` when stacking correlated signals across the same market or sector.

**Rule-based skeleton** (mirrors the trend-gated, confirmed, cost-aware approach the literature supports):

```text
inputs:
    OHLC bars; trend_lookback=10; vol_lookback=20
    confirm=true; max_holding_bars=5
    stop_buffer = 0.1 * ATR(vol_lookback)
    fee_per_trade; slippage_model

for each bar t:
    trend_up   = Close[t-1] > EMA(Close, trend_lookback)[t-1]
    trend_down = Close[t-1] < EMA(Close, trend_lookback)[t-1]

    body  = abs(Close[t]-Open[t]); range = High[t]-Low[t]
    upper = High[t]-max(Open[t],Close[t]); lower = min(Open[t],Close[t])-Low[t]

    bullish_hammer = trend_down and lower>=2*body and upper<=0.25*body
                     and max(Open[t],Close[t]) >= Low[t]+0.7*range
    bearish_star   = trend_up  and upper>=2*body and lower<=0.25*body
                     and min(Open[t],Close[t]) <= Low[t]+0.3*range
    bullish_engulf = trend_down and Close[t-1]<Open[t-1] and Close[t]>Open[t]
                     and Open[t]<=Close[t-1] and Close[t]>=Open[t-1]

    on bullish signal:
        trigger = High[t]
        if confirm and Close[t+1] <= trigger: skip
        entry = next_open_or_stop_above(trigger)
        stop  = min(Low over pattern bars) - stop_buffer
        size  = risk_size(entry, stop)
        exit on first of: time_stop(max_holding_bars),
                          target(entry + 2*(entry-stop)),
                          opposite_signal
        record net PnL AFTER fees and slippage
    on bearish signal: symmetric
```

**Patterns as features, not oracles.** If the goal is predictive performance rather than chart literacy, encode patterns as inputs to a broader model (with volume, trend, levels, other indicators) instead of trading them mechanically. That's where the more convincing modern results come from.

## Use by trading style

- **Discretionary swing trader** — prioritize *confirmed* two- and three-candle reversals on daily bars near real support/resistance, where practitioner logic is clearest and noise is lowest.
- **Systematic trader** — treat single-candle shapes mostly as categorical *state features* or filters, not standalone signals; always validate with costs and multiple-testing corrections.
- **Intraday trader** — assume any apparent edge under ~1–2 ticks/pips/spreads per trade is untradeable once latency, spread, and slippage are modeled.
- **Cross-asset researcher** — avoid gap-heavy pattern definitions in 24-hour/24-7 markets unless "gap" is redefined for that market's microstructure.

Bottom line to give users: rigorous but not cynical. Candles are valuable as a shared language for price action, for disciplined entries/stops, and as structured features in bigger models — much less so as universal standalone alpha. The more statistical rigor applied, the more the "edge" tends to shrink toward conditional and often post-cost-insignificant.
