# Pre-registration — Closing Card Stretched-Body Half Size

**Registered:** 14-Oct-2026, before any live night was graded under it.
**Status:** LIVE as an advisory size suggestion from the first restart after
this commit. Never a skip, never a verdict change.

## Rule (frozen)

A **CLEAN** closing card issued after a day body of **≥ 300 points**
(|15:00 price − today's open|, the module's registered `tiers.STRETCH_PTS`)
carries a **halved size suggestion** (`size_factor = 0.5`) and the note
*"HALF SIZE — stretched day body"*. Any tier or no tier. FLAGGED and
INCOMPLETE cards, unknown bodies, and bodies under 300 are full size.

- The body threshold is **not a knob** — it is the same frozen constant as the
  crash-day label (registered 09-Oct), so label and size cannot drift apart.
- The factor is a knob only for the off-switch: `CLOSING_STRETCH_SIZE_FACTOR`
  (default 0.5; 1.0 disables). Any other value is a new registration.

## Why 300 and not 250 (the question that chose the number)

Replay over the study's 72 clean nights (18-Sep-2025 → 13-Oct-2026):

| Rule | Nights hit | Year total | Max drawdown | Worst losing streak |
|---|---|---|---|---|
| Full size (status quo) | 0 | +2,106% | −348% | −291% |
| Half size, body ≥ 250 | 11 | +2,037% (−3%) | −316% | −259% |
| **Half size, body ≥ 300** | **7** | **+2,129% (+1%)** | **−316%** | **−259%** |
| Half size, body ≥ 350 | 5 | +2,151% | −316% | −259% |

The entire drawdown benefit comes from the 300+ nights; the 250–300 band
(4 nights, 75% win rate) only costs return. 300 is where the relief is free.

## What it would and would not have caught (stated honestly)

- **08-Oct-2026** (GOLD PE, body −396, −77%): halved.
- **12-Oct-2026** (SILVER PE, body −259, −92%): **not caught** — below 300.
  Accepted on the evidence above; a rule that catches it costs more than it saves.
- **28-Sep-2026** (SILVER PE, body −268, +184%): untouched at 300 (halved at 250).

## Why a size rule and not a filter

The clean cell is right-tail driven: the top 5 nights of the year carried 72%
of its P&L. Every skip variant tested (skip ≥250, skip the gap-down+PE setup)
cost 7–28% of the year because it deletes the nights that pay. The separator
scan across all 17 big-down-body PE nights found no 15:00 input that tells the
−92% nights from the +330% nights (best single split 71% vs a 59% base rate).
The losers are the price of the setup; the rule manages the price.

## Grading

Forward only. The study ledger already records the body (`gap_pts`) per
night, so the readout is: clean nights with |body| ≥ 300 **after 14-Oct-2026**,
compared on realised P&L distribution against the year's seven such nights
(57% WR, catastrophic tail). Read at ~10 forward stretched nights (historically
~7 a year — this is a slow experiment, said up front). Outcomes written down
now: (a) the tail persists → the rule stays; (b) stretched nights turn out
benign → factor to 1.0 and this document records why.

## Risks

- Halving also halves any stretched-night winner (the 06-Mar +330% was a
  stretched night). The replay says the net is still favourable; the forward
  sample decides.
- The replay's premiums are Black-Scholes-modelled for ~half the nights (Kite
  deletes expired contracts); real prints exist only from Sep-2026 and have
  agreed with the model within a few percent so far.
