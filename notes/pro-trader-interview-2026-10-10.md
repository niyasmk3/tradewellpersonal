# Pro-trader interview (Trader Table, option buying) — rules, and what our data says

Source: https://www.youtube.com/watch?v=PhvkL2LR0Sg (transcript supplied 10-Oct-2026).
House rule applied: every claim is a HYPOTHESIS. Measured where our data can;
adopted only as dataset features or pre-registered studies; never as a live
rule change.

## The trader's rules (condensed)

1. **Right market beats precise entry.** Buy options aggressively above
   India VIX 15; below 13 don't buy at all; 13–15 selective. In 5 years you
   get "maybe a couple of months" of the right market — they pay for the rest.
2. **Capital management:** keep only the day's deployable money in the
   trading account; withdraw profits; after a loss roll back to base size;
   three consecutive losing days → take a break; NEVER add capital intraday.
3. **Overtrading early warning:** rising transaction charges / order count
   over a few days precede the big loss day. Watch it; cut trades.
4. **Edge audit:** if profits come from averaging down losers, the edge is
   fake and will blow up. Winners must be made at normal size.
5. **Exits:** scale out 30–50% at higher-timeframe resistance; add back
   (next strike if psychology bites) only after the breakout SUSTAINS; exit
   on the "blast" (a spike in your favour — stops get hit, latecomers pile
   in, reversal follows); exit on a swing break; time-stop if consolidation
   at resistance lasts 1.5–2h without a new high. Stops are read off the
   underlying, not the premium.
6. **Sizing = the whole risk:** deploy only what you can lose entirely that
   day (e.g. ₹10k of ₹2L); then the premium stop is unnecessary — wide
   stops get hit, small size doesn't. 40/40/20 staged entry.
7. **Events (RBI/budget/elections):** never buy before; wait for IV collapse
   and the initial reaction; read the full statement, not the headline.
8. **Expiry day:** avoid buying; if forced, ITM with a sliver of OTM, size
   under the daily risk.
9. **Low-VIX hack:** stay sharp with tiny iron condors; never scale buying
   in low VIX; keep a separate low-VIX strategy, never abandon the high-VIX
   setup.
10. **Bias:** from monthly/weekly/daily structure; ATR for the expected
    range; patterns over indicators; sharp pullback = reversal, flat
    retracement/VCP = continuation; >1h flat without breakout = fading.
11. **Edge proof:** backtest for a fair idea, forward-test with small real
    size for the truth.

## What our data says (10-Oct-2026)

- **Rule 1 is real on NIFTY.** 1,267 sessions by prev-close VIX: big-move
  days (range >1.5%) are 3.0% below VIX 13 vs 32.7% above 18 (~11x); avg
  range 0.74% vs 1.37%. Now a permanent table on the research page.
- **Most of our paper era is buyer's winter.** Of 130 graded cards, 95 were
  born below VIX 13 (the trader's "don't buy" zone), 28 at 13–15 and 7
  above 15 — all seven in October, when VIX climbed to 15.2. (First draft
  of this note said "every card at VIX 11–12": that came from a daily VIX
  table that had silently stopped updating on 19-Aug. Fixed the same
  evening — see PROGRAM.md "DATA INTEGRITY (10-Oct)".) Recorded as a
  standing caveat: every model verdict carries the VIX mix of its sample.
- **Rule 8 (expiry day) is NOT supported by our 121 cards:** Tuesday wins
  42.9% (best weekday), Thursday 20% (worst). n=21–34 per weekday — hints.
- **Rules 2, 3, 4, 6** describe the human book, where our evidence already
  agrees: the 2-lot trades held both worst losses; 1-lot trades earned 5x
  per lot; the ₹57k overnight exposure was the "add intraday" sin.
- **Rule 5** is a family of exit counterfactuals — queued for pre-registered
  study (blast exit, time-stop, scale-out), not applied.
- **Rule 9** is literally the condor module plus the gold lab: already built.
- **Rule 11** is the house methodology verbatim.

## Adopted today

- Features: `dte_at_entry`, `is_expiry_day`, `event_flag` (scheduled-event
  warning; 0 of 136 cards so far), `holds_overnight` (late-day gap warning;
  13 cards), `htf_ret_20d_pct`, `vix_at_birth` (exact 5-minute VIX at birth).
- Data fix: daily VIX/index series now extend nightly from local data; the
  VIX feature's same-day look-ahead removed.
- Research page: VIX-regime table + the "buyer's winter" caveat.
- PROGRAM.md: six queued hypotheses + the low-VIX caveat on every verdict.
- Not changed: any live rule, any .env sizing knob (the "full premium at
  risk" sizing is a user decision, pre-registered as a comparison).
