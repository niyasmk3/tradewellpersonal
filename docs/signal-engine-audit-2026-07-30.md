# Tradewell Signal Engine — Deep Audit (evidence through 30-Jul-2026)

**Scope & method.** Three evidence layers, each graded for what it can honestly support:

| Layer | What it is | Size | Limits |
|---|---|---|---|
| **A — Live record** | Signal archive with per-component scores + paper book (real option premiums, slippage, full Zerodha charges) + live journal | 7 trading days (24–30 Jul), 43 paper fills, ~30 archived cards | Full score (incl. OI/news). Small. Archive begins 27-Jul; 21–23 Jul cards were destroyed by pre-archive restarts. |
| **B — Replay** | The *actual production engine* (`regime.classify` + `scoring.py`) replayed over Kite 3m futures history at six score gates | 43 sessions, 5,375 bars, 1,384 replay trades, 225 mechanically-defined opportunity events | **Technical-only score** (OI/news don't exist in history; ÷70×100 rescale). Grades the underlying — no theta/IV/spreads, so real option results are *worse*. Conservative fills (next-bar open, stop-first, slippage both sides). No cadence throttle modelled. |
| **C — Code** | Line-verified decision map of every rule, threshold, weight, and gate | 30 findings with file:line cites | Static truth, no outcomes. |

Every claim below is verified against these (34 analyst findings raised, 16 survived independent numeric verification; several more had **correct numbers** but were rejected for framing caveats, which are incorporated). Where a question exceeds the evidence, the report says so instead of answering.

**Live config at audit time** (matters for everything): `STOP_PRIMARY=premium` (disaster-backstop branch inert), `SIGNAL_MODES=intraday,positional` (**scalp disabled by user edit Wed 29-Jul 17:39** — its 50-fill live-gate evidence is frozen at 10), throttle 10/day · 600s gap · 300s cooldown · 1800s flip-guard, `TRADING_CAPITAL=500000` @1%. **The three week-1 gates (14:15 cutoff, +5% early de-risk, re-fire guard) were committed 28-Jul 17:43 but the backend process predates them — zero live fires through 30-Jul.** Wed/Thu are therefore clean *pre-gates baseline* days.

---

## 1. Executive Summary

1. **The confidence score is not a ranking. It's a gate at best.** Across 1,384 replay trades, win rate is flat (34–39%) from gate 62 to 82, profit factor is U-shaped with its *worst* values mid-range (0.59–0.62 at 70–74), and score bands show no ordering — the 60–63 band (58.8% WR, +0.37R) *beats* the 84–87 band (30.0% WR, −0.38R). The two **perfect-100** technical scores in 43 sessions **both lost**. Live data agrees: cards issued at 78–88 show no score-outcome ordering. Cause is structural (§9): the regime vote and the score re-count the same six facts, so "tradeable regime + score≥78" is nearly one condition, and everything in it is trend-lagging.
2. **The measured economics are zero-to-negative on every layer.** Replay: PF < 1 at all six gates (best 0.76). Paper book (real premiums, net of charges): 43 fills, ~32% WR, positive only via one +58.8% outlier; excursion curve now shows **no premium-move level above +5% with positive expectancy** (and +5% itself flipped from +1.34% to −0.08% when 8 trades were added — the sample is that fragile). Priority order is therefore: **fix per-trade economics first, scale capture second.** Capturing more of a ~zero-EV signal stream captures nothing.
3. **Recall is capped at ~18% and the score threshold is not the reason.** Of 225 mechanical opportunity events (≥40pt move ≤45min, ≤12pt adverse first), the best gate captures 17.8%. Counterintuitively, capture *rises* as the gate tightens (11.1% @62 → 17.8% @78) — a **slot-occupancy artifact**: loose gates burn the single position slot on marginal trades and then sit occupied through the real move. The live system stacks *more* dead time on top (600s gap, 300s cooldown, 1800s flip-guard — none modelled in the replay), so live capture is likely *below* 18%.
4. **The missed opportunities are blind spots, not near-misses.** Of 41 missed events in the last 10 sessions at gate 78, only **5 had any same-direction candidate at any gate down to 62**. The other **36 (88%) produced no candidate at all**: 9 fall in the structurally blind 09:15–10:00 warm-up (15×3m candles required — zero replay trades ever fire in the 9-o'clock hour at *any* gate), and the remainder are regime-vote lockouts (the engine only ever acts in the regime-bias direction; a corroborated example: 30-Jul 12:33 PE move missed while the engine issued a CE card at 12:36) or non-tradeable regime labels. **Threshold tuning cannot touch 88% of the recall problem.**
5. **Time-of-day is a more consistent signal than the score itself — and it contradicts one of our own new rules.** Hour 10 is the *worst* entry hour at all six gates (−0.46 to −0.66R, n=25–52). Hour 15 is the *best* at all six gates (+0.10 to +0.35R). This is the most gate-invariant pattern in the replay, and it challenges the just-shipped 14:15 entry cutoff (built on n=7 live cards). Caveat that keeps the cutoff alive: the replay grades the underlying and **cannot see theta**, which is precisely the late-day killer for bought options — and the live n=7 evidence was real premiums. Verdict: cutoff stays *provisionally*, demoted to "unproven hypothesis," with a shadow counterfactual to settle it (§16 P0-2).
6. **"Strong" regimes are the most consistently losing regimes.** strong_bullish and strong_bearish are negative at *every* gate (n=47–62 each) — more robustly negative than any other bucket. A "strong" label requires the move to be well-developed (vote ≥4, ADX ≥23), which is the profile of exhaustion, not onset. The regime classifier works as a describer; as a *permission slip for chasing* it is harmful. No regime bucket with n≥30 shows a robust positive edge, so regime-conditional *inclusion* gating is unsupported — but regime-based *caution* (strong = size down / demand pullback) has the best-supported direction in the dataset.
7. **Three code-level defects materially distort signals** (§14): (a) the volume participation floor **passes missing data and vetoes mediocre-but-real data** — "volume unavailable" scores exactly 7.0, the floor is `<7`, so a blind tape sails through while a real 1.0–1.19× ratio (6 pts) is vetoed; (b) strike liquidity guards (min OI 100k, spread ≤1.5%) are **silently bypassed** by an ATM fallback — an illiquid strike ships with no note; (c) **every entry gate is issue-time-only** — an adopted card (positional: 6 hours) keeps being served through conditions that would veto a fresh card (stale feed, cutoff, exhaustion), and the veto reason is discarded while the slot is held.
8. **Several assumed features do not exist.** No MACD, no GIFT Nifty/global cues, no previous-day high/low (plumbed but never populated), no opening-range logic in the live engine (only in an offline backtest that *lost* in all 5 configs over 58 sessions), no multi-timeframe confirmation, no per-strike IV in scoring (computed for display only), no setup taxonomy of any kind. The engine is, precisely: one-timeframe trend-confirmation scoring with derivative and news seasoning.
9. **What is actually working:** the participation-floor *concept* (all 4 sub-floor cards to date lost — 2 as hollow counterfactual fills, 2 as real pre-floor fills; small n but 100% directionally consistent), the exit ratchet (beats bank-at-quick-target by ₹2,343 on diverged fills so far), stop-width placement (18% sits near the 80th percentile of observed MAE — defensible), the evidence machinery itself (counterfactual books, paired A/Bs, archive), and the throttle *as a capture aid* (see #3).

---

## 2. Current Signal Engine (verified decision map)

**Flow:** ticks → 3m/15m candles (session-scoped; forming candle dropped) → indicators (VWAP session-anchored, EMA 9/20/50, RSI-14, ATR-14, ADX-14, supertrend 10/3, volume ratio vs 20-bar mean, structure over 10 bars) → `regime.classify` (7-fact directional vote: VWAP side, EMA9v20, EMA20v50, supertrend, structure, RSI 55/45, prev-close side; plus compression/ATR-spike specials) → **both directions scored** on the identical facts (100 pts: price action 25, trend 20, volume 15, OI 20, volatility 10, news 10) → the **regime bias picks the only actionable direction** → ~20 ordered gates (warm-up, calendar, score≥78, extension ≤3.5 ATR, strike pick + liquidity, premium freshness ≤120s, invalidation room ≥1 ATR, session-frame, post-gap quiet, cutoff*, re-fire*, friction*, participation floors→hollow shadow) → throttle (10/day, 600s gap, 300s cooldown, 1800s flip-guard) → one card per (symbol, mode) slot → monitor (t0 +12% → SL to entry; dead-zone ratchet 55% above QT; T1 trail; invalidation latch; 15:20 time exit; 45m stall) → expiry/supersession → append-only archive. (*committed, not yet live-deployed.)

**Notable dead/inert paths:** `BREAKOUT_DEVELOPING` and `REVERSAL` regime labels are unreachable; `RegimeResult.strength` computed and unused; `opening_range()` has zero callers; prev-day H/L always None; disaster-SL branch inert under `STOP_PRIMARY=premium`; scalp profile entirely dead in current config; candlestick patterns clamped to [−3,+4] *and* absorbed by the 25-pt price-action clamp — bonuses vanish exactly when the chart is already perfect, penalties always land.

**Where the LLM sits:** only news classification (headline → sentiment/impact via Claude), feeding ≤10 of 100 points. No LLM in the decision path itself. Hallucination risk is bounded to the news component and further bounded by the 6-hour window and ±5-point swing.

---

## 3. Historical Performance

**Replay (43 sessions, technical-only, underlying-graded — option results would be worse):**

| Gate | Trades | /day | WR% | Avg R | PF | Max DD (R) | Capture |
|---|---|---|---|---|---|---|---|
| 62 | 299 | 6.95 | 39.1 | −0.151 | 0.76 | 21.7 | 11.1% |
| 66 | 274 | 6.37 | 36.5 | −0.216 | 0.67 | 25.4 | 9.8% |
| 70 | 250 | 5.81 | 34.4 | −0.254 | 0.62 | 26.5 | 10.7% |
| 74 | 210 | 4.88 | 35.2 | −0.273 | 0.59 | 25.7 | 15.1% |
| **78 (live)** | **183** | **4.26** | **38.8** | **−0.167** | **0.74** | 20.2 | **17.8%** |
| 82 | 168 | 3.91 | 39.3 | −0.157 | 0.75 | 19.7 | 15.6% |

**Live paper book (real premiums, net of all charges), 23–30 Jul:** 43 fills, 32% WR, net positive *only* via one +₹6,573 outlier (without it: negative). By exit type (n=31 snapshot): target1 avg **+₹1,476** (9/9 wins) · time_exit **−₹165** · stall **−₹456** · invalidation **−₹487** · stop **−₹591**. exit_ab (n=33, 4 diverged): ratchet −₹184/trade vs quick-bank −₹255/trade.

**Precision/recall, honestly stated:** treating the 225-event universe as ground truth, the deployed configuration has recall ≈ 17.8% and per-signal precision ≈ 38.8% winners (technical replay) / ≈32% (live premiums) — but with average loss > average win at most gates, **precision-as-win-rate overstates quality; expectancy is the honest precision metric, and it is ≤0 everywhere measured so far.** The one positive expectancy pocket: replay hour-15 entries and the live target1-exit class.

---

## 4. Winning Signal Analysis

- **All 9 live paper wins ≥ target1 shared one signature: participation.** Winners' median volume component 15/15; the two biggest replay-band positives (60–63, 76–79) are small-n but the *live* winners (11:57, 12:12 on 24-Jul; 4 scalp wins Mon 27) clustered 10:00–14:25 with volume 10–15/15 and OI ≥ 14/20 — and, tellingly, **moderate price-action scores (14–17/25)**. The chart wasn't "perfect" yet; the move had fuel left.
- **"Cancelled" cards outperform "expired" cards massively** (47% vs 9% WR on the week-1 sample; 3 of the 4 biggest wins were later *superseded* cards). Supersession usually means acceleration, not error. Do not treat cancellation count as a quality metric.
- The one +58.8% outlier (23-Jul positional PE) rode a multi-day thesis through a −26% drawdown first — it exists *because* nothing de-risked it early. This is the tension the +5% early de-risk must be measured against (§12).

## 5. Losing Signal Analysis

Individual losing signals, live paper book (each verified):

| Time | Contract | Score | vol/oi/pa | Outcome | Net | Post-mortem |
|---|---|---|---|---|---|---|
| 24-Jul 10:03 | 23600 PE | 81 | **2**/20/25 | stop | −₹1,381 | Perfect chart, dead tape. Would be floor-vetoed today. |
| 24-Jul 12:51 | 23850 CE | 82 | **6**/20/25 | invalidation | −₹1,239 | Same signature. Floor now catches it. |
| 27-Jul 10:39/10:40 | 23850 PE + 23950 PE | 82/84 | 10/16, 10/16 | stops | −₹1,364 | Same thesis fired twice in 60s across modes; both died together. Re-fire guard (not yet live) addresses the *repeat*, not the first. |
| 28-Jul 13:16→15:06 | 24000 PE ×3 | 80–83 | ok | 2 stops + time | −₹829 | Thesis re-fired twice after first stop. The exact re-fire-guard case. |
| 27–28 late cluster | 7 cards ≥14:27 | 79–84 | ok | 5 time_exit, 2 stops | −₹1,416 | No runway before 15:20. The cutoff case — but see §11's conflicting replay evidence. |
| 29-Jul all day | 5 CE entries | 79–83 | ok | stalls/invalidations | −₹1,116 | **Right direction, wrong minutes**: the day's 3 real moves (09:57 CE, 12:09 PE, 13:42 CE) were all missed; entries landed in the chop between them. Entry-onset problem, not direction or strike. |

**Pattern:** losers are (a) participation-hollow charts (fixed by floors), (b) thesis repeats (fix pending deploy), (c) no-runway late entries (contested), and — the biggest residual — (d) *right-context, wrong-moment* entries produced by threshold-crossing on lagging confirmation.

## 6. Missed Opportunity Analysis

41 events ≥40pts in the last 10 sessions had no gate-78 signal within −6/+15 min. Root-cause classification:

| Cause | n | % | Fixable by threshold? |
|---|---|---|---|
| **No candidate at any gate ≥62 — engine blind** | 36 | 88% | **No** |
| — of which: 09:15–10:00 warm-up (structural, zero trades ever fire pre-10:00) | 9 | 22% | No — design |
| — of which: regime-vote lockout / non-tradeable label / slot busy (not yet separable) | 27 | 66% | No — needs instrumentation (P1-2) |
| Candidate existed below/near gate ("almost") | 5 | 12% | Partly |

Selected individual missed trades (replay-verified; entries are the event-trigger bar):

| Time | Setup context | Dir | Tradewell | Entry ref | Move | Why missed | Fix path |
|---|---|---|---|---|---|---|---|
| 17-Jul 09:30 | Opening drive | CE | NO SIGNAL | 24,274 | ≥40pt | Warm-up blind zone (needs 15×3m candles) | Deliberate opening module or accept (ORB tested: lost) |
| 17-Jul 14:39 | Trend continuation | CE | NO SIGNAL | 24,376 | ≥40pt | Candidate scored **88.6** (would have won +0.51R) — slot/cadence occupied | Slot turnover, not threshold |
| 22-Jul 12:15 | Breakdown leg | PE | NO SIGNAL | 24,082 | ≥40pt | Candidate 75.7 (< 78) — won +1.44R in replay | One of only 5 genuine near-misses |
| 29-Jul 09:57 | Post-open breakout | CE | NO SIGNAL | 24,259 | ≥40pt | Warm-up | as above |
| 29-Jul 12:09 | Reversal leg | PE | NO SIGNAL | 24,314 | ≥40pt | Regime locked CE all day — PE never scored for action | Dual-direction scoring (P1-2) |
| 30-Jul 12:33 | Counter-trend spike | PE | NO SIGNAL (CE card issued 12:36) | 24,30x | ≥40pt | Regime-bias lockout, corroborated by the archive | Same |

## 7. Almost-Signal Analysis

Only 5/41 recent misses were near-misses, and the aggregate score-band evidence **argues against rescuing them by lowering the gate**: the 68–71 band is among the worst in the whole distribution (28.9% WR, −0.37R, n=38), and 72–75 is also negative (−0.27R, n=37). The near-miss zone is a bad neighborhood on average, even though 2 of the 5 individual near-misses would have won. **Verdict: keep gate 78. The threshold is roughly as good as any tested value and no tested value is good — the leverage is elsewhere.** (User-format table in §3.)

## 8. Root Cause Matrix (bad + missed, ranked by evidence-weighted impact)

| # | Root cause | Hits | Evidence grade |
|---|---|---|---|
| 1 | Score aggregate uninformative above ~62 (regime/score double-count; lagging stack) | every signal | B+C, high |
| 2 | Blind spots: warm-up hour + single-direction regime lockout | 36/41 misses | B, high |
| 3 | Slot/cadence occupancy caps capture at ~18% | all misses | B, high (mechanism), unmeasured live |
| 4 | Entry-onset timing: threshold-crossing after the move (hour-10 chase = worst hour at all gates) | ~35 replay trades/gate + Wed-29 case | B, high |
| 5 | Participation-hollow charts pass on price-action strength | 4 cards, all lost | A, small-n consistent |
| 6 | Late-day no-runway entries (theta-adjacent; contested by replay hour-15) | 7 live cards | A(n=7) vs B conflict — unresolved |
| 7 | Volume-floor missing-data inversion (blind tape passes) | latent, every no-volume tick window | C, certain |
| 8 | Issue-time-only gates on held cards (positional 6h worst) | latent | C, certain |
| 9 | Silent strike-liquidity bypass | unknown frequency | C, certain mechanism |
| 10 | Thesis re-fires after stop | 5 cards week-1 | A, small-n |

## 9. Market Regime Performance

Replay avg R by regime (all gates consistent): **strong_bullish negative at every gate** (−0.14…−0.54, n=47–52), **strong_bearish negative at every gate** (−0.10…−0.44, n=53–62), moderate_bullish negative at every gate (n=30–51), moderate_bearish positive *once* (gate 62, +0.06) and negative at all stricter gates. news_volatility positive everywhere but n=1–2 (same trades persisting — an anecdote, and it's an ATR-spike proxy, not real news). **Conclusions:** (1) no inclusion-style regime gate is supportable; (2) the *best-supported* regime rule in the entire dataset is an **exclusion/caution on STRONG labels** — the classifier's highest-conviction states are its worst trades, because "strong" ≈ late; (3) regime's correct V2 role is risk-manager (sizing/patience modifiers), not permission-granter.

## 10. Setup Performance

**The engine has no setup taxonomy — this is itself the finding.** Nothing in the code knows whether a card is a breakout, a pullback, a VWAP reclaim, or a range fade; everything is one trend-confirmation score. Consequently setup-level performance cannot be measured from live data, and the missed-move analysis (§6) shows the cost: moves whose *onset* looks like a reversal or a fresh breakout score poorly on trend-confirmation until they've already run. Building setup detection (with per-setup expectancy tracked in the archive from day one) is the single largest *recall* lever available that does not degrade precision — it adds candidates the current engine literally cannot see, each gated by its own participation floors. Graded P1-experimental: high potential, zero historical validation yet, must ship behind the paper book like scalp did.

## 11. Entry Timing Analysis

- **Structural floor:** 3m candle close + next-bar entry = 3–6 minutes / ~7–13% of a 45-minute move given away by construction. Acceptable in isolation; fatal when stacked on lagging confirmation.
- **The chase signature is measurable:** hour-10 is the worst entry hour at all six gates — the engine wakes at ~10:00 (post warm-up) into moves that began 9:15–9:45 and buys their tails. The strong-regime negative results (§9) are the same phenomenon wearing a different label.
- **The late-day conflict, stated honestly:** live n=7 says ≥14:27 entries all lost (real premiums, theta-bearing); replay says hour-15 is the best hour at every gate (underlying-graded, theta-blind, EOD exits). Both are true of their own measurement. The deployed 14:15 cutoff therefore rests on the weaker sample but the more realistic instrument. Resolution shipped as P0-2: keep the cutoff, but log every would-have-been card 14:15–15:10 into the hollow shadow book so the paper book (which *does* pay theta) accumulates the real counterfactual. Decide at 30+ shadow fills, not on n=7 vs n≈15.
- **WATCH→CONFIRMED lifecycle:** no persistence requirement exists today — a single bar crossing 78 becomes a card. The recorded cards show threshold-flicker clustering (multiple same-thesis cards minutes apart). A two-stage lifecycle (WATCH at score-wait, CONFIRM on persistence + participation) is directionally supported but **unvalidated** — P2, must be replayed first.

## 12. SL & Target Analysis

- **Stop width (18%) is not the problem:** observed MAE distribution (43 fills) has median −8.0%, p80 −15.2%, worst −25.6% — the stop sits ~p80–p90. Defensible.
- **Which quantity governs the stop is unresolved and has been flip-flopped on n=1 twice** (`underlying` 21-Jul morning → `premium` 21-Jul evening, both single-trade decisions; the codebase's own 30-sample rule was never applied to its most consequential risk parameter). P1: build the paired stop-basis counterfactual (same machinery as exit_ab) and let it run to 30 diverged fills.
- **T1 (+27%) and T2 (+45%) are dead letters** — reached ≤11.6% and ~5% of the time; the calibrator knows this (P75-MFE clamps to [8%,27%]) but is inert below 30 clean samples (currently ~20). When it does fire it will land near +8–12% and **silently null the quick target** (T1 < QT ⇒ QT dropped — code-verified), collapsing the two-stage exit the ratchet depends on. P1 guard required before calibration activates.
- **Early de-risk (+5%, committed, not yet live)** is the highest-expected-value exit change: 53.5% of fills touch +5%; every level above it is negative in expectation. Its risk is documented honestly: the book's single biggest winner drew down −26% before +58% — a +5% breakeven rule would have killed it. The paper book will price this trade-off; the rule ships measurable either way.

## 13. Options Contract Analysis

Direction error vs option-selection error, separated where evidence allows: the Wed-29 case study shows the **direction read was right** (2 of 3 real moves were CE, the engine was CE all day) and the **entries missed the windows** — strike selection was never implicated (liquidity-guarded ATM/OTM picks, momentum-based bias). No historical option-chain data exists to audit IV expansion/contraction or spread costs beyond the paper book's realized fills, which already carry the full charges model (~17–30% of gross at scalp cadence — the friction veto exists for this). **Two real defects:** the silent ATM fallback past liquidity guards (P0-3), and theta-blindness of every underlying-graded evidence source — which is exactly why the late-day question must be settled by the premium-paying paper book, not the replay.

## 14. Signal Quality Matrix

| Problem | Impact | Frequency | Evidence | Fix |
|---|---|---|---|---|
| Volume floor passes missing data (7.0 = floor) & vetoes real 1.0–1.19× | Blind-tape cards ship uninspected | Every no-volume window | C, certain | P0-1 |
| Aggregate score non-informative | Mis-placed confidence everywhere | Always | B high + A | V2 scoring (P1-1) |
| 88% of misses = blind spots | Recall ceiling | 36/41 | B high | P1-2 instrumentation → setup detection (P1-4) |
| Slot occupancy caps capture | Recall ceiling | Structural | B high | P1-3 measured cadence replay first |
| Issue-time-only gates on held cards | Stale cards served (positional 6h) | Latent | C certain | P2-1 |
| Silent strike-liquidity bypass | Untracked slippage risk | Unknown | C certain | P0-3 |
| Strong-regime chasing | Worst trades at all gates | n≈50×2 | B high | P2-2 sizing/caution |
| Late-day cutoff contradiction | Possibly discarding best hour | 7 vs ~15 | A/B conflict | P0-2 shadow counterfactual |
| STOP_PRIMARY n=1 flip-flops | Unmeasured risk basis | 2 flips | A+C | P1-5 paired A/B |
| Calibration will null quick-target | Future silent exit collapse | Pending n≥30 | C certain | P1-6 guard |

## 15. Tradewell Signal Engine V2 (evidence-derived architecture)

The audit's shape dictates the design: **floors and vetoes work here; weighted aggregates don't. Detection is the bottleneck; confidence theater is the surplus.**

- **L1 Context & health** (exists, keep): session/calendar, feed freshness, post-gap quiet, warm-up honesty.
- **L2 Regime as risk-manager, not gatekeeper** (change): regime stops granting permission and instead sets *posture* — strong labels ⇒ reduced size / pullback-required; sideways/unsafe ⇒ stand aside (keep); direction lockout **removed at the scoring layer** (both directions always fully evaluated and *actionable*, with the flip-guard as the whipsaw brake).
- **L3 Setup detection** (new, the recall engine): explicit, named detectors — breakout of range/day-extreme, VWAP reclaim/reject, pullback-to-EMA20 in trend, failed-breakout reversal, opening drive (deliberate, separate, given ORB's negative history) — each emitting a candidate with its own geometry. Every candidate logged; untraded candidates flow to the shadow book. *This* is where "earlier but not reckless" lives: onset conditions are per-setup, not global trend confirmation.
- **L4 Participation floors as necessary conditions** (exists, fix P0-1, extend): volume and OI floors per candidate; missing data fails closed with its own tag.
- **L5 Geometry & economics gate** (exists, unify): invalidation-room, extension, friction vs T1, spread/liquidity as *hard* guards (kill the silent fallback).
- **L6 Timing/persistence**: WATCH state at candidate detection (pushed as heads-up), CONFIRM on persistence + participation — replay-validated before live (P2-3).
- **L7 Continuous validity**: held cards re-checked against L1/L4/L5 each cycle; a card that would now be vetoed is WEAKENING → retired (P2-1).
- **L8 Confidence, demoted and honest**: a *label* derived from which floors passed with how much margin — displayed, archived, never summed into a fake 100.
- **L9 Slot & cadence**: measured redesign (P1-3) — candidate-aware slot policy (a held marginal position can be recommended-closed when a fresh A-grade candidate appears), loss-aware cooldowns.
- **L10 Evidence loop** (exists, the project's crown): every new gate ships with its shadow counterfactual; every knob has a decision sample size; nothing flips on n<30.

## 16. Prioritised Implementation Plan

**P0 — defects & contradictions (small, this week)**
1. Volume-floor missing-data inversion: missing volume must fail closed (distinct tag), real 1.0–1.19× passes review. *Files: scoring.py, service.py. Effect: precision ↑ slightly, honesty ↑ certainly.*
2. Late-day shadow counterfactual: keep the 14:15 cutoff live, but book 14:15–15:10 would-be cards into the hollow/shadow store tagged `late`; decide at 30 fills. *Resolves finding #5 with premium-truth.*
3. Strike-liquidity fallback: make guards hard (AVOID with reason) or ship the bypass with a loud card note. *strike.py.*
4. Ops: deploy the dormant gates (one ⟳ restart) and make a conscious call on scalp (currently off; its 50-fill gate is frozen at 10 — either resume evidence or retire the mode).

**P1 — high-impact (next 2 weeks, each with its measurement)**
1. Scoring V2 core: strip the regime/score double-count; both-directions actionable; floors-first architecture (L2/L4/L8). Re-run this audit's replay before/after.
2. Blind-spot instrumentation: log bull+bear scores & veto states at every bar; classify the 27 unattributed misses; sizes the setup-detection prize precisely.
3. Cadence replay: extend the backtest with live gap/cooldown/flip-guard and sweep *cadence* at fixed gate 78 — the capture experiment the evidence says matters most.
4. First two setup detectors (breakout, VWAP reclaim) shadow-only → paper.
5. STOP_PRIMARY paired A/B (30 diverged fills before any flip).
6. Calibration guard: T1 ≥ 1.5× quick-target floor before the calibrator can activate.

**P2 — medium**: continuous card validity (L7); strong-regime caution posture; WATCH→CONFIRM persistence (replay-validated); score-band × hour decomposition; per-setup archive analytics.

**P3 — experimental**: opening-window module (only after P1-2 shows the 9 warm-up misses are worth it — ORB already failed once); news-volatility regime exploitation (n=2 anecdote today); BANKNIFTY expansion.

**Projected effects (to be measured, not asserted):** P0s are correctness, not performance claims. P1-1/P1-4 target recall on the 36-blind-spot class (theoretical ceiling: 88% of misses) with precision protected by per-setup floors; P1-3 targets the ~18% capture ceiling. Every change re-runs the same 225-event/43-session framework plus the live paper book — before/after tables come from those reruns, not from this report's optimism.

---

*Corrections to earlier informal claims, for the record: the hollow counterfactual is 2/2 losses (the "4/4 sub-floor" figure combines those 2 with 2 pre-floor real fills — all four lost, but they are different measurement classes). The +5% excursion expectancy flipped sign (+1.34% → −0.08%) with 8 added fills — treat all excursion-derived numbers as provisional below ~60 fills.*

*Generated 30–31 Jul 2026 from: 43-session engine replay (audit_replay.json), live archive/paper/journal evidence, and a line-verified decision map. 34 analyst findings raised, 16 numerically verified; findings rejected for framing are incorporated as caveats. No production signal logic was changed for this audit.*
