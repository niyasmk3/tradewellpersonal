# Closing Day Strategy — findings, 19-Aug-2026

The rule, exactly as tested:

> At 15:00 IST compare NIFTY to **today's open**. Below it → buy the ATM PE;
> above it → buy the ATM CE. Sell at 09:50 the next trading day.
> One lot (65 qty) every night, no filters, no stop.

The reference started as *yesterday's close* — the original hypothesis — and
was changed to *today's open* on the evidence in "Picking PE vs CE" below.
Both modes remain implemented (`CLOSING_SIGNAL_MODE`) and the tab reports them
side by side, because this is a change of hypothesis, not a tuned parameter.
The headline table immediately below is the ORIGINAL reference, kept so the
comparison stays legible; the current default's numbers are in the comparison
table further down.

Signal read from the 15:00 bar's **open** (the 3pm print); fill at that bar's
**close** (15:05), so the trade pays for the five minutes it takes to act.
Exit at the 09:50 print. Nearest weekly expiry that is still alive the next
morning — on expiry day the trade rolls a week.

## The headline

**The directional hunch is real and stable. The ATM-option expression of it is
not established either way.**

| | 1 year (244 nights) | 3 years (734 nights) |
|---|---|---|
| Index continued overnight | **55.3%** | **55.0%** |
| Median signed move | **+15.5 pts** | **+11.5 pts** |
| Mean signed move (95% CI) | +3.8 (−17.1 … +25.2) | +7.6 (−3.6 … +17.7) |
| Option win rate | 43.0% | 38.8% |
| Option mean / trade (95% CI) | −1.4% (−10.2 … +8.0) | −2.5% (−7.9 … +2.5) |
| Option median / trade | −8.8% | −12.5% |
| Total, 1 lot | −₹45,407 | −₹2,33,211 |

Monthly, over the year: mean −₹3,493, median **+₹4,363**, 7 of 13 months
positive, ~19 trades a month. Mean premium outlay ₹8,173 a night; ₹16,189 of
the year went to charges alone.

## What is actually established

1. **The 55% continuation is the finding.** It holds at 55.3% over one year and
   55.0% over three, and it survives the clock sweep — every entry between
   15:00 and 15:20 paired with every exit between 09:15 and 11:00 lands in
   51–55%. The chosen 09:50 exit is genuinely the best cell of that grid
   (55.3%, median +15.5 pts), not a lucky one. This is measured from real index
   prints; no model touches it.

2. **~15 points of median drift does not pay for an overnight ATM option.** The
   ATM premium averages ~126 points. One night of theta at 1–7 DTE, plus half
   the bid-ask each way, plus ₹66 of charges, costs more than 15 points of
   expected drift. Hence a 43% win rate on a signal that is right 55% of the
   time: the index goes the right way and the option still bleeds.

3. **Neither mean is distinguishable from zero.** Every confidence interval in
   the table above spans zero. Over one year this rule is a coin flip with a
   thumb on the scale, not an edge you can bank on.

## The tail is the whole distribution

Median −8.8% but best +419% and worst −101%. Most nights lose a slice of
premium; a handful of gap days pay for a quarter. That is a long-option payoff,
not a income strategy — and it means the year's result is decided by three or
four nights, which is why the monthly table swings from −₹30,203 (Nov-25) to
+₹17,658 (May-26).

The user's own motivating example is in the ledger and it worked: 17-Aug PE
returned **+124%** modelled (the real quote moved +167%), 18-Aug PE **+47%**.
Both are tail nights, not the norm.

## Cuts worth knowing

- **Gap size**: `<25 pts` −14.2% mean, `25–50` −22.0%, `50–100` +6.5%,
  `>100` +2.6%. A quiet afternoon is the worst signal — you pay full premium
  for a whisper.
- **Direction**: CE −8.7% mean over 128 trades, PE +6.6% over 116. PE carries
  the entire year, and it does so through the Feb–Apr 2026 selloff.
- **Entry VIX**: `>20` is −21.0% mean over 19 trades. Buying overnight premium
  when it is already expensive is the losing corner.
- **DTE**: `0–1` (exit lands on expiry morning) −4.5% mean, median −19.2% —
  the highest-variance bucket, and the one the eye-catching examples come from.

## What is measured vs modelled — and how wrong the model is

Kite deletes expired option contracts: an expired token returns
`invalid token`, so a year of real premiums cannot be fetched at any price.
Layer 2 therefore prices ATM options with Black-Scholes on real spot and real
India VIX, with two things fitted from the only real NIFTY option quotes on
this machine (app/condor's chain snapshots, 6 sessions):

- **carry 11.75%/yr**, from put-call parity — well above the 6.5% risk-free the
  rest of the codebase carries. Ignoring it overprices puts ~25% at 1 DTE,
  a directional bias in a directional study.
- **ATM IV / VIX ratio by DTE**: 1.51× on expiry day, 0.97× at 1 DTE, ~0.85×
  a week out.

Measured error:

| check | result |
|---|---|
| Premium level, leave-one-session-out | **9.1%** median abs error |
| Premium level, in-sample | 6.0% (vs **19.4%** uncalibrated) |
| Overnight % change — the study's own metric | **12.1pp** median abs error, n=12 real pairs |
| Overnight bias | **−5.8pp** — the model *understates* the real move |
| Overnight sign agreement | 83.3% |

**The result sits inside its own error bar.** Removing the measured −5.8pp
bias moves the year from −₹45,407 to **+₹69,753** (+4.3% mean, 9/13 positive
months) — and that corrected mean *still* spans zero. The option-layer
conclusion is undetermined; the index-layer conclusion is not.

**Trust boundary**: the model was calibrated over VIX 11.25–11.98. **82.8% of
these trades, carrying 87.7% of the absolute P&L, fell outside that range.**
The Feb–Apr 2026 selloff — where the biggest wins and losses live — is entirely
extrapolated.

## Assumptions that could not be verified here

- **Weekly expiry weekday.** Thursday before 2025-09-02, Tuesday after. The
  live instrument dump confirms the Tuesday half; nothing in this repo can
  verify the pre-switch leg. Config: `CLOSING_EXPIRY_SWITCH`.
- **No skew.** CE and PE share one vol, so PE results are marginally optimistic
  and CE marginally pessimistic.
- **Lot size 65 throughout.** It has changed historically; rupee figures scale
  linearly, percentages do not move.
- **Previous close** is the last free-tape print, with the CAS freeze and its
  auction print stepped over structurally. On 17-Aug this reads 24,339.6
  against an official 24,338; taking the last bar would have read 24,287.

## Picking PE vs CE — the reference matters more than anything else

Fourteen candidate rules scored on the signed overnight move (index points
only, no option model), across a 2-year in-sample stretch and a 1-year holdout.
Ranked on **skill** = mean − (CE share − PE share) × unconditional drift, which
strips out what a rule earns simply for being net long a rising index.

The control that reframes everything: **ALWAYS BUY CE** earned +6.86 pts/night
over 3 years at a 55.2% hit rate — statistically indistinguishable from the
original rule's +7.56. *The "vs yesterday's close" rule is barely doing
anything beyond being long.* It picks CE on 54% of nights, and that tilt is
where most of its apparent edge comes from.

| rule | n | CE share | hit | skill 3y | 2y in | 1y out |
|---|---|---|---|---|---|---|
| vs open, \|body\| < 50 pts ✓ | 238 | 41.6% | 54.2% | **+20.9** | +16.0 | +30.5 |
| **vs today's open** | 734 | 47.5% | 54.2% | **+15.1** | +11.8 | +21.9 |
| open AND prev close agree | 576 | 51.2% | 55.9% | +14.0 | +12.6 | +16.9 |
| upper half of day's range | 734 | 49.9% | 54.1% | +11.6 | +9.4 | +15.8 |
| vs close two days back | 734 | 53.8% | 54.0% | +10.3 | +12.0 | +6.3 |
| vs 14:00 (last hour) | 734 | 48.8% | 53.0% | +9.2 | +6.6 | +14.8 |
| **vs previous close** (original) | 734 | 54.4% | 55.0% | +7.0 | +8.4 | +3.7 |
| this morning's gap ⚠ | 734 | 58.7% | 53.4% | +1.5 | +5.9 | −8.6 |
| ALWAYS CE (control) | 734 | 100% | 55.2% | 0.0 | 0.0 | 0.0 |
| yesterday's overnight ⚠ | 734 | 55.3% | 52.5% | −0.9 | +2.7 | −8.4 |
| FADE today's open | 734 | 52.5% | 45.8% | −15.1 | −11.8 | −21.9 |

⚠ = skill flips sign between in-sample and holdout, which is what noise looks
like. Day-of-week and VIX conditioning (tested and not shipped) flipped the
same way.

**Recommendation: compare the 15:00 print to TODAY'S OPEN, not yesterday's
close.** Green day → CE, red day → PE. It roughly doubles the skill (+15.1 vs
+7.0), it holds in both the in-sample and holdout windows, and — the part that
matters — it splits CE/PE ~48/52, so what it earns it earns from being right
rather than from the drift.

Carried into the option layer:

| rule | window | win | mean | total (1 lot) |
|---|---|---|---|---|
| vs yesterday's close | 1y | 43.0% | −1.4% | −₹45,407 |
| vs yesterday's close | 3y | 38.8% | −2.5% | −₹2,33,211 |
| **vs today's open** | 1y | 43.9% | **+4.0%** | **+₹97,177** |
| **vs today's open** | 3y | 38.0% | −0.8% | −₹42,308 |

Better in every cell, and over three years it turns a −₹2.3L hole into roughly
breakeven. It still does not clear the bar: every confidence interval spans
zero, and the median night still loses ~8%. A better side-picker does not fix
the fact that one night of ATM theta costs more than the drift is worth.

`|body| < 50` was the only candidate clearing zero on both windows, but it was
selected out of 14 and it only trades a third of nights. Treat it as a
hypothesis to pre-register, not a result.

**What is missing and probably matters more than any of this:** the overnight
gap is driven largely by global cues — the US close and GIFT Nifty, which
trades until 02:45 IST. None of that is in this dataset, and GIFT Nifty is not
available through Kite Connect. Every rule above is trying to predict an
overnight move from Indian daytime data alone.

## Headline under the new default (vs today's open)

| | 1 year (244 nights) | 3 years (734 nights) |
|---|---|---|
| Index continued overnight | **56.6%** | **54.2%** |
| Median signed move | **+17.5 pts** | +10.6 pts |
| CE share (drift exposure) | 48.8% | 47.5% |
| Option win rate | 43.9% | 38.0% |
| Option mean / trade | **+4.0%** | −0.8% |
| Option median / trade | −7.8% | −13.8% |
| Total, 1 lot | **+₹97,177** | −₹42,308 |
| Months positive | 8 / 13 | — |

Monthly on the year: mean **+₹7,475**, median +₹6,237. With the model's
measured −5.8pp bias removed, the year is +₹2,10,234 and 9/13 months positive.

Every caveat above still applies unchanged: the 95% CI on the mean is
[−6.0, +15.2] and spans zero, the median night still loses, and 82.8% of the
P&L sits outside the calibrated VIX range.

## Round 3 (19-Aug, evening): can the side-pick be made more reliable?

Motivated by 2026-08-14 — day green at 15:00, CE bought, index fell overnight.
First, the honest frame: **13 of the 15 rules tested said CE that night and
lost.** The only same-night winners (fade, always-PE, yesterday's overnight)
are long-run losers. No side-picker fixes individual nights; 44% of nights look
like 14-Aug under any rule that is right 56% of the time.

But 14-Aug had a tell: the day was green while the **last hour was falling**
(14:00 at 24,395 → 15:00 at 24,374). Testing that family plus new information
sources (India VIX day move, VWAP position, NSE-listed US proxies MON100 /
MASPTOP50), 15 candidates, same harness:

| rule | skill 3y | 2y in | 1y out | verdict |
|---|---|---|---|---|
| **body & last-hr AGREE** | **+19.1*** | +14.6 | **+28.6*** | best; clears zero pooled AND holdout |
| body & MON100 agree | +19.6* | +17.2* | +23.4 | good, but needs ETF data; holdout CI wide |
| day_open (incumbent) | +15.1* | +11.8* | +21.8* | stable |
| body & VWAP agree | +14.3* | +8.3 | +25.8* | stable, weaker |
| disagree → follow last-hr | **−8.0** | −7.0 | −9.8 | consistently NEGATIVE — do not fade |
| VIX day move / vs prev close | ~0 | − | + | noise |
| MON100 / MASPTOP50 alone | +7 / +5 | + | + / − | weak or noise |

**The finding:** when the day's body and the last hour disagree, the night is
not predictable — following the last hour instead *loses* in every window. The
right move on a 14-Aug-shaped day is **no trade**, and that is exactly what the
confirmation rule does (it stood aside on 14-Aug).

Option layer with the agreement filter (day_open, trade only when the last
hour agrees — 63% of nights):

| | all nights | filtered | skipped nights alone |
|---|---|---|---|
| 1y total | +₹97,177 | **+₹98,563** (n=155) | −₹1,386 |
| 3y total | −₹42,308 | **+₹37,563** (n=465) | **−₹79,871** |
| 3y mean/trade | −0.8% | **+1.4%** | −4.6% |

Same money, one-third fewer trades, and the 3-year option total turns positive
for the first time under any rule tested. The filter's entire effect is
removing the toxic subset.

**Caveats, stated plainly:** this candidate was found by inspecting a specific
losing night, so its holdout number is partially self-selected — the mitigation
is that the in-sample window (which contains nothing of 14-Aug) also scores
+14.6, and the rule is now pre-registered in `signals.py` (`lasthr_confirm`)
to be judged on nights it has never seen. All CIs above remain uncorrected for
15 candidates × 3 windows. And the structural ceiling stands: the overnight gap
is substantially made overnight (US session, GIFT Nifty), which nothing
readable at 15:00 IST contains — the US-proxy ETFs' failure here is evidence of
exactly that.

## Round 4 (19-Aug, late): filters on the confirmed Overnight set

46 filters tested across two batteries (25 hand-built + 21 from a three-lens
ideation pass), each scored on option net% with a random-removal percentile
control, split 2y-in-sample / 1y-holdout, then the top five adversarially
verified by independent agents (statistics lens + mechanics/concentration
lens). Base: the 465 confirmed Overnight trades, +1.38%/trade over 3y.

**The one verified survivor: VOL EXPANSION.** Trade only when India VIX at
15:00 is above its previous close OR above its own 09:15 open. Verified
CONFIRMED by both lenses: reproduces exactly, seed-stable, sign-consistent in
three consecutive periods (both in-sample halves + holdout), survives a
46-filter family-wise correction (joint FWER 0.027), improvement positive on
BOTH the CE and PE legs, not concentrated in any month or any handful of
nights — and the edge shows up model-free in the underlying itself (kept
nights' signed spot move +36.7 pts vs +18.5 removed). Mechanism has an
economic prior: a long ATM option is long vega, and buying it while vol is
being bid means expansion pays part of the theta bill.

| confirmed Overnight + | n (3y) | mean/trade | total | maxDD | trades/mo |
|---|---|---|---|---|---|
| (nothing) | 465 | +1.38% | +₹37,563 | −₹1,35,074 | 12.9 |
| vol expansion | 252 | **+10.75%** | **+₹1,58,022** | **−₹39,653** | 7.0 |
| vol expansion, +0.1 buffer | 225 | +12.06% | +₹1,54,774 | −₹37,268 | 6.2 |
| vol expansion + mid-range | 219 | +14.36% | +₹2,02,492 | −₹37,005 | 6.1 |

Holdout year, fully stacked: n=73, win 50.7%, mean +23.4%, median finally
positive (+0.3%). Half-spread both ways and full Zerodha charges are inside
every number. Live use should apply a +0.1 VIX-point buffer: 44/155 holdout
classifications sat within 0.15 pts of the raw threshold.

**Near-survivor: MID-RANGE EXCLUSION** (skip when the 15:00 print sits
0.35–0.65 of the day's range). Threshold-robust (every band 0.25–0.75 through
0.45–0.55 improves both windows), mechanism visible (the 0.4–0.6 deciles run
−13%/−27% mean — a direction printed from mid-range is a direction the market
has not committed to), benefit spread across 24/33 months — but on its own it
falls short of family-wise significance. Use as the second layer, not alone.

**Refuted — and this is the round's real lesson.** The best-LOOKING filter,
"last hour moved ≥20 pts" (99.3rd percentile on holdout; its combos hit 99.6),
was killed by the statistics lens: indistinguishable from random removal
in-sample, sign FLIPS across in-sample halves, the holdout glory is five
lottery-ticket nights (97% of the kept total), and it dies under the 46-test
correction. Also refuted or weak: strict VIX-above-prev-only (top-3-night
concentration), VWAP-distance ≥25 (PE leg value-destroying), theta caps,
weekend-carry exclusions, day-of-week, streaks, premium-cost caps, flat VIX
level cutoffs, and post-loss skips — all flip between windows.

**Wired into the Overnight tab (19-Aug, late).** Both filters are registered
in `app/overnight/filters.py` (REGISTERED_ON = 2026-08-19, band and thresholds
frozen; tests pin them so they cannot drift silently). They render as FLAGS,
never gates: the headline strategy ignores them, every ledger row shows V/R
chips, the backtest ladder is displayed with what each rung removed, and a
live scoreboard grades only nights strictly after the registration date —
verdict at 30+ filtered live nights, per the house rule.

**Standing caveats.** The holdout is spent: 46 filters were mined against it,
so every number above is a HYPOTHESIS to pre-register and judge on live
nights, not a result to bank. The dataset still cannot see event calendars or
global cues (vol expansion may partly proxy event eves; the model-free spot
check argues against a pure IV artifact, but n is small). NSE's CAS (live
03-Aug-2026) changed close mechanics, and these features were fit almost
entirely on pre-CAS tape. The kept median night still loses on most cuts —
the tails still carry everything — and the ±12pp model error on overnight
premium changes applies to every option number here.

## Honest summary

The hunch found something: NIFTY does lean the way its 3pm direction points,
about 55% of nights, worth a median 15 points, consistently across three years
and robust to the exact clock — and it leans harder when the reference is
today's open rather than yesterday's close. What it did not find is a way to
collect it by buying an at-the-money option overnight — that expression spends more on theta
and spread than the drift is worth, and the modelled P&L is dominated by a
handful of gap nights in a volatility regime the pricing model has no evidence
about.
