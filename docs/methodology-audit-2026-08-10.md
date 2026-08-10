# Methodology Audit — Market-Structure / VWAP / Breakout-Retest vs Tradewell (10-Aug-2026)

**Question:** would the proposed methodology (higher-TF structure, VWAP alignment, EMA trend,
key levels, breakout validation, retest, volume confirmation, ATR/extension filters, R:R
filter, regime detection) improve Tradewell's signal quality?

**Answer: PARTIALLY — and mostly not in the proposed form.** Of the twelve proposed concepts,
seven are **already implemented** inside the engine, three are **vacuous or damaging** when
added as filters (they cannot remove what the gate already requires), and exactly **one
concept family survived walk-forward validation**: *trade only while the day is one-sided but
unspent* ("developing tape", optionally with a volume trim). Even that edge is thin,
top-day-dependent, theta-blind, and earns only a shadow-ledger trial — not production. The
audit's most valuable outputs are three findings *nobody proposed*: a toxic time-of-day
window, an anti-calibrated confidence score, and proof that the exit bracket — not entry
direction — destroys the engine's participation in major moves.

**No production code was changed by this audit.**

---

## 0. Methodology & data honesty

- **Harness:** the established technical-proxy replay (3-min near-month futures, the live
  `regime.classify` + `_technical_score`, gate 78, swing stop, 1.5R target, slippage against,
  EOD close). 52 sessions (27-May → 10-Aug), 469 gate-crossing episodes, 621 candidate rows,
  ~30 features per row computed with zero look-ahead. Arm A = enter first gate bar
  (pre-confirm baseline). Arm B = enter after 2nd consecutive gate bar (current production).
- **Walk-forward:** DEV = first 31 sessions (27-May → 10-Jul), TEST = last 21 (13-Jul →
  10-Aug), rules frozen on DEV, verdicts read on TEST.
- **Live truth:** 56 clean paper fills (net of charges, 23-Jul →), signal archive, eval
  trace, last week's real-minute-premium audit.
- **Hard limits stated up front:** all R figures are on the underlying proxy — theta-blind
  and before ~₹75/lot charges, so every expectancy shown is optimistic. Option-chain history
  does not exist (OI/IV predictiveness = NEEDS DATA). VIX join failed in the matrix (null on
  all rows) — VIX segmentation not run. Premium-level truth covers ~1 week only.

## 1. Current architecture (Phase 1)

Fifteen-step pipeline, tick → card: market-hours gate → closed-bar frame + indicators
(VWAP, EMA 9/20/50, RSI, ATR, ADX, Supertrend, BB width) → **regime classifier**
(7-input directional vote; compression/sideways/ATR-spike/VIX special cases; ADX floors 16/20)
→ dual 0–100 scoring (direction comes from regime, score only qualifies) → score gates
(70/78 intraday) → exhaustion veto (>3.5 ATR from EMA20) → strike selection (ATM±1, OI +
spread guards) → premium freshness gate (120s) → price ladder + invalidation-room veto
(≥1 ATR) → sizing → integrity vetoes (frame/gap/volume-health/scalp-friction) → measured
vetoes (participation floor, re-fire guard, 14:15 cutoff, WATCH→CONFIRM) → setup detectors
(shadow) → cadence throttle (4/day, 15-min gap, flip guard). Exits: invalidation → stop →
T2 → time exit → T1/trail → quick-target ratchet → early-derisk → stall exit.

**Already-implemented equivalents of the proposal:** VWAP alignment (regime vote + 8 score
pts), EMA trend (vote + 12 pts), HH/HL structure (vote + 9 pts), breakout of swing/day
extreme (8 pts), relative volume (the 15-pt volume component IS relative volume), ATR
extension (exhaustion veto), regime detection (regime.py), R:R floor (invalidation-room +
min-risk), momentum (RSI in vote + score). **Not implemented:** PDH/PDL/ORB/CPR key levels
in the signal path (they live in the Patterns module), breakout-retest sequencing,
VWAP-pullback entry (exists only as the shadow setup detector).

## 2. Baseline (Phase 2)

| Metric | Arm A (445) | Arm B / production (176) |
|---|---|---|
| Win rate | 37.8% | 36.9% |
| Expectancy | −0.189R | −0.190R |
| Profit factor | 0.71 | 0.69 |
| Avg MAE / MFE | 0.95R / 0.91R | similar |
| Reach 0.5R / 1R / 1.5R / 2R (MFE) | 57 / 42 / 29 / 9% | — |

- **There is no positive baseline to protect.** Filters must remove ~0.19R/trade of drag.
- Adverse excursion exceeds favorable on average; 58% take 0.5R heat before showing 0.5R
  of profit; **give-backs are only 9.4%** — losses are trades that never worked, not
  winners surrendered.
- **Time-of-day is the sharpest segment: 10:30–12:00 is toxic** (n=102, WR 24.5%, −0.47R,
  PF 0.43); 12:00–14:00 is nearly breakeven (−0.06R). No signal has ever fired before 09:57
  (indicator warm-up).
- PE outperforms CE (−0.08R vs −0.31R). Trend days −0.04R vs range −0.25R / mixed −0.41R.
- Gap and expiry segments: no meaningful discrimination.
- The confirm gate (arm B) cuts signal count ~60% **without improving per-trade expectancy
  in this window** — and halves major-move participation (see §6). Its live ledger remains
  the binding arbiter, but proxy evidence is now 2-of-3 against.

## 3. Why signals fail (Phase 3, live rupees)

On the 40 losing clean fills: **immediately-reversed = 57% of loss rupees** (29 of 40 losers
die inside 15 minutes — the flicker/impulse-chase signature); **stop-too-tight proxy = 31%**
(MAE just past the stop, several recovered after); **gave-back = 27%** (MFE ≥ +5% then red);
late-day entries 16%; theta-bleed 7%; expiry-day 4%. (Tags overlap.)

## 4. Score calibration (Phase 13)

**The confidence score is currently anti-calibrated above 80.** Buckets (live fills):
75–79 → 38% WR, ≈breakeven; 80–84 → 24% WR, −₹206/trade; 85+ → 36% WR, −₹196.
Score ≥80 overall: 27% WR vs <80: 33%. A user seeing "85" is not getting a better trade
than "76". Small n (52 scored fills), but direction is consistent: **the extra points above
~78 come from components that do not predict outcomes** (volume/OI/VIX pile-on during
already-extended moves). → Scoring V2 (deferred P1-1) should be re-prioritized once trace
data suffices; until then treat score magnitude above the gate as noise.

## 5. Ablations (Phases 4–5) — one filter at a time, both arms

| Proposed filter | Verdict | Evidence |
|---|---|---|
| VWAP alignment | **VACUOUS** | removes 1 of 445 (A), 0 of 176 (B) — gate-78 already implies it |
| EMA 9>20 alignment | **VACUOUS / damaging** | removes ≤10 rows; *incremental* after structure: removes exactly 1 row — a winner |
| 15m structure (strict HH/HL match) | **Best broad trim, still negative** | A: −0.19 → −0.145 (eff 0.62); B DEV gain **collapsed in TEST** (+0.169 → −0.405) |
| Structural R:R ≥ 1.0–2.0 | **REJECT** | damaging on A at every threshold (removes the biggest winners); B-only gain did not walk forward (MODEL_B: sign flips at n=7/2) |
| Breakout validation (close/body/vol) | **NEEDS DATA** | only positive cell is n=6 — noise; pool too small |
| Breakout retest | **NOT TESTABLE / unstable** | distance proxy flips sign across arms; true sequencing needs a bar-path detector |
| Relative volume ≥1.5 | **Valid small trim** | positive in all 4 arm×period cells, +0.008…+0.067R — a trim, not an edge; eff 0.77 on B |
| ATR-extension cap (≤1.0–1.5) | **REJECT — premise inverted** | median entry is already 3.1 ATR from VWAP; ext ≤1.0 keeps the *worst* subset (−0.43R); extended entries are *different, not worse* |
| Consecutive-candle cap | **REJECT** | removal efficiency 0.50 = coin flip; no threshold exists |
| Range-spent cap ≤1.0 | **REJECT** | weak (eff 0.54); worst band is *early tiny-range* days, not late |
| **Developing tape (0.35 < sf < 0.60)** | **THE KEEPER** | the only filter positive in **all four** arm×period cells (Δ +0.07…+0.35R); flips arm A positive in both DEV (+0.046) and TEST (+0.117 without aligned) |
| Day-alignment (with the day's side) | **REJECT — subtracts value** | A −0.196, B −0.271 vs −0.19 baseline; developing+aligned < developing alone in TEST (+0.062 vs +0.117) |

## 6. Missed opportunities (Phase 14) — the audit's biggest reframe

171 major moves (≥50 pts / 90 min) in 51 sessions. **Capture rate: 19% (arm A), 8% (arm B).**
But the deeper finding: **captured moves still lose — median −1.05R** — because the 3-bar
swing stop gets hunted before 100+ point moves complete and the 1.5R cap truncates the rest.
Only 16 of 138 missed moves had *no* right-direction candidate all day: **the engine sees
the direction; the bracket loses the trade.** 38% of missed moves start before 10:00, where
warm-up means zero signals ever fire; candidate volume instead peaks 12:00–13:00 — when
moves are rarest, and inside the toxic 10:30–12:00 shoulder. Chasing late (beyond +45 min)
also loses (median −1.01R). **Conclusion: entry-filter work cannot fix this; stop/target
design (the running stopb/stopc/exit ledgers) and the 09:15–10:00 blind spot are where major
moves are actually lost.**

## 7. Combinations + walk-forward (Phases 6, 15)

- MODEL A (structure+VWAP): direction confirmed on A, still negative; B collapsed in TEST.
- MODEL B (+R:R): unstable noise at untradeable n. Reject.
- MODEL C (developing+aligned): **only pre-specified model confirmed** on arm A both periods
  (+0.046/+0.062R, PF 1.08/1.12, ~1.5 sig/day) — but the *aligned* leg subtracts value, and
  ex-best-day the edge goes negative in 3 of 4 cells. Thin, fragile, real.
- MODEL D (all DEV-passing filters ANDed): kept 1 DEV trade, 0 TEST trades — a textbook
  demonstration that stacking DEV-screened filters selects noise.
- Post-hoc best cell: developing + rel_vol≥1.5, arm A TEST: **+0.459R, WR 63.6%, PF 2.25,
  n=22, survives ex-best-day (+0.397)** — but post-hoc, arm-divergent, and August (CAS era)
  is its worst month (−0.469, n=11). Shadow-ledger candidate only.

## 8. Options layer (Phase 12)

Selector is ATM±1 with OI/spread guards. From live fills: the strong-momentum **OTM step is
the worst realized bucket** (18% win, −4.3%/trade — more paper MFE, less banked money);
2–4 DTE is the only positive expiry bucket; expiry-day fills bleed ~10× more per trade in %
despite the same win rate; "meaty" premiums (0.7–1.2% of spot — the positional holds) are
the only positive premium band. Theta on 0–2 DTE never tested (discipline exits everything
<60 min). **NEEDS DATA (concrete logging gaps):** per-strike IV is computed live but never
persisted; StrikePick carries OI at selection but the card drops it; spreads unlogged.

## 9. Final classification (Phase 18)

**KEEP (already in the engine — do not duplicate):** regime gates, exhaustion veto,
structure/VWAP/EMA/volume as score components, invalidation-room floor, cadence throttle.

**DO NOT ADD (tested, failed):** VWAP-alignment filter, EMA filter, structural-R:R hard
filter, ATR-extension cap, consecutive-candle cap, range-spent cap, day-alignment
requirement, retest requirement (in proxy form).

**TRIAL VIA SHADOW LEDGER (pre-registered, in priority order):**
1. **Developing-tape condition** (0.35 < sf < 0.60, *without* alignment) — the one
   walk-forward survivor. Infrastructure already ships (tape state is stamped on every card
   since 09-Aug); the golden-label ledger will grade it live. **Audit recommends evaluating
   golden's `aligned` leg for removal at the ledger's first read.**
2. **rel_vol ≥ 1.5** as a second condition (valid trim in all cells).
3. **10:30–12:00 caution window** — new finding, −0.47R bucket; size a time-of-day shadow
   veto on more history before any gate.
4. **Drop the OTM step** on strong momentum (worst realized bucket) — cheap config A/B.
5. **Log IV, OI, spread on every card** (pure logging; unlocks Phase-12 questions forever).

**RE-PRIORITIZE (bigger than any proposed filter):** (a) stop/target design — captured
major moves losing −1.05R median says the bracket, not the entry, forfeits the big days;
the stopb/stopc/exit A/B ledgers already in flight are the vehicle; (b) scoring V2 —
the confidence score is anti-calibrated above 80; (c) the 09:15–10:00 warm-up blind spot
(38% of major moves start there) — needs a design, e.g. seeded indicators or an
opening-specific setup, sized before build.

## 10. Bottom line

The proposed methodology's core instincts — trade with structure, don't chase, respect
levels, demand volume — are **already encoded** in Tradewell's score and gates; adding them
again as hard filters is redundant at best and harmful at worst (they delete winners at the
same rate as losers). The genuinely new, validated information in the proposal reduces to
one sentence: **the engine should be most active while the day is one-sided but unfinished,
and quiet otherwise** — which is now measurable live through the tape-state tags. Everything
else this audit surfaced points away from entry filters and toward exits, score calibration,
the mid-morning dead zone, and the opening blind spot. NO TRADE remains a valid output; on
this evidence, it is the *correct* output more than half of every day.

*Generated by the 8-agent audit workflow, 10-Aug-2026. Matrix: 52 sessions / 621 candidates.
No production logic was modified.*
