# Iron Condor Module — Phase 2 Strategy Specification (11-Aug-2026)

Status: **PROPOSED — awaiting user approval before any implementation.**
Companion to `docs/iron-condor-audit-2026-08-11.md` (Phase 1). Every constant below is
a config default (`CONDOR_*` in `.env`), not a hard-code. House rules applied:
deterministic and auditable, floors-first (hard gates decide; the score displays),
shadow-first deployment, pre-registered thresholds, NO TRADE is the default output.

## 0. Adopted assumptions (from Phase-1 Q&A, reversible)

| Q | Assumption |
|---|---|
| Execution | Advisory-only. No 4-leg Kite basket in v1; user executes manually in Kite and journals the fills. |
| Instrument | NIFTY, nearest weekly expiry. BANKNIFTY behind a config flag, off. |
| DTE | Allowed 2–7 (`CONDOR_MIN_DTE=2`, `CONDOR_MAX_DTE=7`). 0–1 DTE blocked with a gamma warning. |
| Margin | Estimate = `width × lot_size × CONDOR_MARGIN_FACTOR (2.0)` − net credit, labelled ESTIMATE. Factor raised 1.0→2.0 at review: 1.0 reduces to bare max loss, ~2-4x under real NSE SPAN+exposure. Kite `basket_order_margins` upgrade later. |
| Backtest | Premium-true expectancy claims deferred until snapshot data accumulates; range-call backtest on the 3-y spine ships in Phase 5. |
| Snapshots | 5-min chain snapshots to SQLite from day one (§10). |
| Underlying basis | Prices/Greeks off index spot with `r = RISK_FREE = 0.065` (basis folds into IV — consistent with `options/iv.py`; documented bias). Regime/tape math off the near-month future (house convention). |

## 1. Notation

`S` spot LTP · `F` future LTP · `T` = `years_to_expiry(expiry)` (ACT/365 to 15:30 IST)
· `σ_K` = per-strike implied vol (bisection, from leg **mid** where depth exists, else
LTP) · `Δ_K, Γ_K, Θ_K, ν_K` = Black-Scholes Greeks at strike K · `step = 50` ·
`lot` = per-contract lot size from the instrument dump · `EM` expected move (§4) ·
All bar math on **closed** candles of the near-month future; 5m is the working frame,
15m the context frame.

Black-Scholes Greeks (new `condor/greeks.py`, pure functions over `iv.bs_price` inputs):

```
d1 = [ln(S/K) + (r + σ²/2)T] / (σ√T)        d2 = d1 − σ√T
Δ_call = N(d1)          Δ_put = N(d1) − 1
Γ = φ(d1) / (S σ √T)
Θ = [−S φ(d1) σ / (2√T) − r K e^(−rT) N(±d2)] / 365      (per calendar day)
ν = S φ(d1) √T / 100                                      (per 1 vol-pt)
```

`N` = `iv._norm_cdf`; `φ(x) = e^(−x²/2)/√(2π)` (one new line). Position Greeks = signed
sum over legs (short = −1, long = +1) × lot × lots.

## 2. Data-quality gate G0 (hard, fail-closed)

All must pass or the evaluation returns `DATA QUALITY` (with the failing reasons) and
stops. No guessing, no fallbacks.

1. Market open (`mcal.is_market_open`), not in post-gap quiet (`gap_ended_at` + 600 s).
2. Spot/future tick age ≤ 15 s (`MarketState.last_tick_age`).
3. Warm-up: ≥ 36 closed 5m bars today’s session context available on the 15m frame
   (≥ 12 bars) — regime needs persistence history.
4. Per leg (all 4): exchange-timestamped tick, age ≤ `SIGNAL_MAX_PREMIUM_AGE_S` (120 s);
   `ltp > 0`; **bid and ask present** (missing depth ⇒ FAIL, inverting the
   `_spread_ok` fail-open); spread gate §6.5; OI present and ≥ floor §6.5.
5. `σ_K` solvable for both short strikes and within [5 %, 60 %].
6. All legs share the universe expiry; DTE within [2, 7].
7. VIX tick present.

## 3. Market regime model R (condor-specific; `signals/regime.py` untouched)

**Inputs (all exist today):** 7-fact directional vote `net15` on the 15m frame
(recomputed via the same facts as `regime.classify`: VWAP side, EMA9/20, EMA20/50,
supertrend, structure, RSI 55/45, prev-close side) · `ADX15` = ADX(14) on 15m ·
`bbw` = `bollinger_width` on 5m closes vs its trailing 40-bar distribution ·
`ATR5` = ATR(14) on 5m vs its 30-bar median · session VWAP crossings ·
`range_vs_typical` (pulse baseline) · session extremes · VIX status/percentile.

**Range votes V1–V7** (each 0/1, evaluated on closed bars):

| # | Vote | Condition |
|---|---|---|
| V1 | Balanced tape | `|net15| ≤ 2` |
| V2 | Weak trend | `ADX15 < 20` |
| V3 | Trend not building | `ADX15 ≤ ADX15[−6] + 1` |
| V4 | Two-way tape | ≥ 4 sign changes of `(close5m − VWAP)` today, ignoring bars where `|close − VWAP| < 0.1 × ATR5` |
| V5 | Range normal | `range_vs_typical_pct ≤ 110` |
| V6 | Near value | `|F − VWAP| ≤ 1.0 × ATR5` |
| V7 | Extremes respected | no new session high or low in the last 12 × 5m bars |

**Vetoes (any ⇒ regime = VOLATILE / UNSAFE, condor blocked):**
ATR spike `ATR5 > 1.8 × median30` · VIX status = High · VIX intraday change ≥ +5 % ·
post-gap quiet · **COILED**: `bbw ≤ p20(trailing 40)` AND `range_vs_typical ≤ 60`
(compressed coil precedes expansion — the one thing worse than a trend for a condor).

**Labels:**

- `RANGE_BOUND` — `ΣV ≥ 5` **on each of the last 3 closed 15m bars** (persistence is
  the feature the old classifier lacks) and no veto. Historical bars evaluate
  V1–V4, V6, V7 on data sliced to that bar's close (review fix: freezing the
  5m-frame votes at "now" made persistence a 1-bar check in disguise); V5's
  slow-moving baseline is the one documented now-evaluated exception, and a
  missing baseline fails V5 closed.
- `MILD_BULLISH / MILD_BEARISH` — `sign(net15)` with `2 < |net15| ≤ 4`, `ADX15 < 22`,
  ≥ 4 of {V2..V7} true, persistence as above, no veto.
- `TRENDING` — `|net15| ≥ 5` or `ADX15 ≥ 25`. Condor blocked.
- `VOLATILE_UNSAFE` — any veto. Blocked.
- `WARMING_UP` — insufficient bars. Blocked.

**Confidence** (display only, never a gate): `round(100 × (ΣV/7) × min(1, bars_held/6))`
where `bars_held` = consecutive 15m bars the label has held.

**Range levels reported:** `[max(support_strike, L_low), min(resistance_strike, L_high)]`
where `support/resistance_strike` = OI walls (`features.oi_analysis`) and `L_*` =
nearest strong levels from `patterns/levels.find_levels`. Both shown with sources.

## 4. Expected move

Four estimators, all displayed; disagreement is information:

1. `EM_straddle` = ATM CE ltp + ATM PE ltp (the market’s priced move to expiry).
2. `EM_iv` = `S × σ_ATM × √T`, `σ_ATM` = mean of ATM CE/PE IV.
3. `EM_atr` = `ATR_d(14) × √(DTE_t)` — daily ATR from day-aggregated 5-min spine;
   `DTE_t` = trading days to expiry.
4. `EM_rv` = `S × HV20 × √T` — HV20 = stdev of last 20 daily log-returns × √252
   (new function; first realized-vol computation in the codebase).

**Primary safety EM** `EM* = max(EM_straddle, EM_iv)` (conservative). Display
`Expected Range = [S − EM*, S + EM*]`.

## 5. Volatility filter

- `VOL_REGIME`: VIX percentile < 25 ⇒ LOW; 25–75 NORMAL; 75–90 ELEVATED; > 90 EXTREME.
- Gate G3: EXTREME blocked; LOW allowed **only if** credit floor (§6.6) still passes
  (never sell vol just because the chart is flat); ATR-spike / VIX-spike vetoes already
  in §3.
- `IV/RV = σ_ATM / HV20` displayed; ≥ 1.0 scores (§7); no hard floor in v1 (RV history
  thin) — pre-registered for promotion once snapshots accumulate.

## 6. Strike & wing selection

Profiles (`CONDOR_PROFILE`, default `balanced`):

| Profile | Short |Δ| band |
|---|---|
| conservative | 0.10 – 0.15 |
| balanced | 0.15 – 0.20 |
| aggressive | 0.20 – 0.25 |

Algorithm (deterministic, both sides independently, then joined):

1. **Candidates**: strikes with solvable IV and |Δ| inside the band (CE above spot,
   PE below).
2. **EM floor**: distance to spot ≥ `0.75 × EM_iv` (`CONDOR_MIN_EM_DIST=0.75`).
   Under MILD_* regimes the **bias-side** short requires ≥ `1.0 × EM_iv` (skewed
   condor; the drift side gets more room).
3. **Wall snap**: if the OI wall (resistance for CE, support for PE) or a
   `patterns/levels` strong level lies within the band, prefer the first candidate
   **at or beyond** it; else take the band’s outermost candidate that passes floors
   (prefer safety over premium; the credit floor decides if that’s still worth it).
4. **Wings**: width `W` from `CONDOR_WING_WIDTHS = {100,150,200,250,300}`, same both
   sides in v1. Choose the smallest `W` satisfying: wing liquidity floors; net
   `credit/W ≥ CONDOR_MIN_CREDIT_PCT (0.20)`; `max_loss ≤ CONDOR_MAX_LOSS_PER_TRADE`.
   If none qualifies ⇒ NO TRADE (reason: wing economics).
5. **Liquidity floors** (per leg): OI ≥ 100 000 shorts / 50 000 wings
   (`CONDOR_MIN_OI_SHORT/WING`); spread `(ask−bid) ≤ max(0.05 × mid, ₹0.30)` shorts,
   `max(0.15 × mid, ₹0.30)` wings. Missing depth fails closed (G0.4).
6. **Credit economics** (charges included, §8): `credit = (sCE + sPE) − (wCE + wPE)`
   at mids. Floors: `credit ≥ CONDOR_MIN_CREDIT_PCT × W` and
   `credit × lot ≥ 2 × est_charges_roundtrip`.

**Derived card economics:**

```
max_profit = credit × lot × lots − charges
max_loss   = (W − credit) × lot × lots + charges        (same W both sides)
BE_low     = K_shortPE − credit          BE_high = K_shortCE + credit
POP        = N(d2(BE_low; σ_shortPE)) − N(d2(BE_high; σ_shortCE))
margin_est = W × lot × lots × CONDOR_MARGIN_FACTOR − credit × lot × lots   [ESTIMATE]
R:R        = max_profit / max_loss
Net Greeks = signed sums (§1)
```

**Entry zone** (mirrors the existing entry-band philosophy):
`ideal = [0.95, 1.05] × credit_mid` · `minimum acceptable = 0.90 × credit_mid` ·
`avoid below = 0.85 × credit_mid` (recompute at display time from live mids).

## 7. Hard gates, then score

**Gates (any fail ⇒ NO TRADE with the reason listed):**
G0 data quality · G1 regime ∈ {RANGE_BOUND, MILD_*} with persistence · G2 breakout
risk ≤ MEDIUM (§9) · G3 volatility (§5) · G4 credit floors · G5 `POP ≥ 0.60` ·
G6 risk limits: `max_loss ≤ CONDOR_MAX_LOSS_PER_TRADE`, margin_est ≤ available fund,
open condors < `CONDOR_MAX_OPEN (1)` · G7 liquidity floors · G8 entry window
10:00–14:30 IST (`CONDOR_ENTRY_FROM/TO`) · G9 no event inside
`CONDOR_EVENT_WINDOW_H (24 h)` (`.events.json` must be recreated) · G10 DTE ∈ [2, 7].

**Score (0–100, display + threshold only — magnitude is NOT a ranking; lesson from the
anti-calibration finding):**

| Component | Max | Formula sketch |
|---|---|---|
| Regime quality | 20 | `20 × (ΣV/7) × min(1, bars_held/6)` |
| Volatility suitability | 15 | NORMAL 12, ELEVATED 15, LOW 6; +0–3 for IV/RV ≥ 1.0..1.2 (capped 15) |
| Expected-move safety | 15 | `15 × clip((min_dist/EM*) − 0.6, 0, 0.6)/0.6` where min_dist = nearer short’s distance |
| Chain structure | 15 | shorts at/beyond both walls 10; one wall 5; writing flows supportive +3; PCR ∈ [0.8, 1.2] +2 |
| Support/resistance | 10 | both shorts beyond `patterns/levels` strong levels 10; one 5 |
| Premium attractiveness | 10 | `10 × clip((credit/W − 0.20)/0.15, 0, 1)` |
| POP | 10 | `10 × clip((POP − 0.60)/0.20, 0, 1)` |
| Liquidity | 5 | all spreads ≤ half their caps 5; else pro-rata |

Bands: ≥ 80 HIGH QUALITY · 70–79 WATCHLIST (displayed, never pushed) · < 70 NO TRADE.
Configurable (`CONDOR_SCORE_MIN=80`, `CONDOR_WATCH_MIN=70`). Every card lists ✓ reasons
and ⚠ negatives (each component’s inputs, verbatim numbers).

## 8. Charges (extend, don’t overload)

New `condor/charges.py` + `frontend/lib/condorMath.ts` twin (same constants as
`paper/charges.py`): 8 orders round-trip (4 + 4) × ₹20 brokerage; STT 0.0015 × sell
premium turnover (entry shorts + exit buy-side has no STT — STT applies on sell legs:
entry sells 2 legs, exit sells the 2 long wings); exchange 0.03503 %, SEBI, IPFT,
GST 18 %, stamp on buys. Both stores pinned by the same worked-example test pattern.
Expired-worthless variant: 4 entry orders + 0 exits on lapsed legs.

## 9. Breakout-risk score B (0–100 → LOW / MEDIUM / HIGH / EXTREME)

| Signal | Points |
|---|---|
| `ADX15 − ADX15[−6] ≥ 3` | 20 |
| `ATR5 > 1.5 × median30` (25 if > 1.8×) | 15/25 |
| `bbw > 1.3 × min(trailing 20)` | 10 |
| `|F − VWAP| > 1.5 × ATR5` | 10 |
| last-bar volume ≥ 1.5 × median20 | 10 |
| new session extreme within last 6 × 5m bars | 15 |
| VIX intraday ≥ +4 % (20 if ≥ +7 %) | 10/20 |
| OI shift: dominant side’s writing unwinding (Δwriting < 0) over last 30 min | 10 |

Bands: < 25 LOW · 25–44 MEDIUM · 45–64 HIGH · ≥ 65 EXTREME. New entries need ≤ MEDIUM
(G2). Note: the directional engine’s toxic 10:30–12:00 finding is about momentum
*chasing*; chop hours are condor-friendly — no time-of-day veto beyond §7 G8 is
imported. Pre-registered, not tuned.

## 10. Snapshot logging (ships first)

Every 5 min in-session (`CONDOR_SNAPSHOT_S=300`): all subscribed option tokens (both
expiries): `ts, expiry, strike, right, ltp, bid, ask, oi, volume, iv` → SQLite
`backend/.condor_chain.db` (WAL, `INSERT OR REPLACE` on `(ts, token)`); daily summary
row (ATM IV at 09:20/12:00/15:15, straddle, VIX, EM set). ~50 k rows/day ≈ 3–4 MB/day;
retention `CONDOR_SNAPSHOT_KEEP_DAYS=180`; **add to the ops backup glob** alongside
`.patterns_candles.db`. This unlocks IV-rank, IV/RV promotion, and premium-true
evaluation — nothing else in this spec depends on tuning it.

## 11. Position monitoring (manually journaled entries)

User records the 4 fills → `CondorPosition`. Monitor each cycle (30 s):

- Combined mid `C_t`, P&L = `(credit_fill − C_t) × lot × lots − charges_accrued`.
- Distances: `(K_shortCE − S)` and `(S − K_shortPE)`, absolute and ÷ EM_remaining
  (EM recomputed with current σ_ATM, √T_remaining).
- Net Δ/Γ/Θ/ν, B score, VIX.
- **Health (0–100)** = `100 − 35 × clip(1 − min_dist/EM_rem, 0, 1) − 25 × clip(C_t/credit − 1, 0, 1)`
  `− 20 × (B/100) − 10 × clip(|netΔ|/0.25, 0, 1) − 10 × gamma_flag(DTE ≤ 1)`.
  Bands: ≥ 70 Healthy · 40–69 Warning · < 40 Critical.

**Exit statuses (priority order, first match wins):**

1. `EXIT – STOP LOSS`: `C_t ≥ CONDOR_SL_MULT (1.75) × credit_fill`.
2. `EXIT – BREAKOUT RISK`: B = EXTREME, or B = HIGH and `min_dist ≤ 0.3 × EM_rem`.
3. `ADJUST`: threatened short |Δ| ≥ 0.30 or `min_dist ≤ 0.4 × EM_rem` (→ §12).
4. `EXIT – EVENT RISK`: new event inside window.
5. `EXIT – EXPIRY RISK`: expiry day ≥ 14:30 IST with position open (CAS: NIFTY frozen
   15:15–15:35 ⇒ last clean exit ~15:05–15:10; deadline `CONDOR_EXPIRY_EXIT_IST=14:30`).
6. `BOOK PROFIT`: captured ≥ `CONDOR_PROFIT_TARGET_PCT (50 %)` of credit
   (captured = `1 − C_t/credit_fill`).
7. `PARTIAL PROFIT` advisory at ≥ 35 % capture with DTE ≥ 3 (display only).
8. `HOLD`.

UI always states **which rule fired with its numbers**.

## 12. Adjustment engine (deterministic, advisory-only)

Trigger = status ADJUST. Candidates are *repriced from the live chain* (all floors of
§6 re-applied, charges included):

- **A. Roll untested side in**: close far spread, open new spread at current profile
  Δ-band on that side. Gain = added credit; cost = narrower range.
- **B. Close threatened side** (keep untested): caps further loss on the tested side.
- **C. Convert to iron fly**: roll untested short to ATM (defensive credit max).
- **D. Exit entire position.**

**Selection rule**: recommend the highest-credit candidate among A–C that satisfies
*all* of: added credit ≥ `CONDOR_ADJ_MIN_CREDIT (₹8 × lot pts)`; new
`max_loss ≤ 1.15 ×` original; B ≤ MEDIUM after the move; new untested-side short
respects the EM floor. If none qualifies ⇒ recommend **D** with the arithmetic shown.
Adjustment confidence = regime confidence × (1 − B/100). No roll is suggested while
breakout risk is HIGH/EXTREME (adding credit to a structure the tape is leaving). At
most `CONDOR_MAX_ADJUSTMENTS (1)` per position, counted by the user journaling the
executed roll via `POST /condor/positions/{id}/adjust` (which folds the added credit
and the 4 orders' charges into the position); the second trigger ⇒ D. Textbook rolls
are never suggested when the numbers don’t clear the floors.

## 13. What-if analysis

Server-side grid (`POST /condor/whatif`): spot ∈ S + {0, ±0.5, ±1.0, ±1.5} × EM* (and
user-supplied prices), horizon ∈ {now, +1 d, +2 d, expiry}, IV shift ∈ {−10 %, 0, +10 %}.
Reprice each leg via `bs_price(S′, K, T′, σ_K × (1+shift))`; report structure P&L net
of exit charges. Pure function of the card — no market calls.

## 14. History, shadow rollout, and Phase-5 validation

- **Eval trace**: one line per evaluation (`.condor_eval.jsonl`): ts, regime + votes,
  B, EM set, VIX/percentile, gates failed, score components, card id or NO TRADE
  reason. This is the “which conditions produce quality” dataset from day one.
- **Card archive**: full spec’d capture (spot, all strikes/premiums/IVs, credit, EM,
  regime, VIX, ATR, score breakdown, walls/levels, exit reason + timestamp, final
  P&L, combined-premium MFE/MAE) → `.condor_archive.jsonl`.
- **Shadow-first**: cards render on the tab (labelled TRIAL) and archive; **no pushes
  and no “taken” flows** until 30 graded outcomes exist and the ledger is read
  (house 30-sample rule). A sign-aware condor paper grader applies §11 rules to
  archived cards to produce those outcomes.
- **Phase 5**: (a) range-call backtest on the 3-y 5-min spine — pre-registered
  metrics: % of qualifying days where `S ± EM_atr` held to horizon, vs base rate;
  regime-label precision/recall vs realized range days; **no premium claims**;
  (b) premium-true evaluation accumulates from §10 snapshots; graded with the same
  §11 rules. Nothing flips a default without its pre-registered read.

## 15. Config defaults (single .env block)

```
CONDOR_ENABLED=false            CONDOR_SYMBOLS=NIFTY          CONDOR_PROFILE=balanced
CONDOR_MIN_DTE=2                CONDOR_MAX_DTE=7              CONDOR_EVAL_S=60
CONDOR_SCORE_MIN=80             CONDOR_WATCH_MIN=70           CONDOR_MAX_OPEN=1
CONDOR_MIN_CREDIT_PCT=0.20      CONDOR_MIN_EM_DIST=0.75       CONDOR_WING_WIDTHS=100,150,200,250,300
CONDOR_MIN_OI_SHORT=100000      CONDOR_MIN_OI_WING=50000
CONDOR_MAX_SPREAD_PCT_SHORT=0.05  CONDOR_MAX_SPREAD_PCT_WING=0.15  CONDOR_SPREAD_ABS_FLOOR=0.30
CONDOR_MIN_POP=0.60             CONDOR_MAX_LOSS_PER_TRADE=15000    CONDOR_MARGIN_FACTOR=1.0
CONDOR_SL_MULT=1.75             CONDOR_PROFIT_TARGET_PCT=50   CONDOR_MAX_ADJUSTMENTS=1
CONDOR_ADJ_MIN_CREDIT=8         CONDOR_ENTRY_FROM=10:00       CONDOR_ENTRY_TO=14:30
CONDOR_EXPIRY_EXIT_IST=14:30    CONDOR_EVENT_WINDOW_H=24
CONDOR_SNAPSHOT_S=300           CONDOR_SNAPSHOT_KEEP_DAYS=180
```

---

*Every threshold above is a starting default to be graded by the module’s own ledger,
not a claim. Changes to any of them follow the house rule: pre-register, accumulate,
read at n≥30.*
