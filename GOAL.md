# Tradewell — practical goal (written 02-Aug-2026, review 01-Nov-2026)

A one-page charter you can share with the project owner. It states what the
experiment is trying to prove, by when, with numbers — and what it will
never do.

## The goal, in one sentence

> **By 1 November 2026, run 60+ unattended trading sessions, grade 100+
> signal cards on the honest paper ledger, and produce an evidence report
> that says — with walk-forward numbers, not opinions — which gates and
> score components earn their keep, and whether a probability filter can
> lift net expectancy by ≥ +0.05R without dropping more than 60% of cards.**

## What "automatically run" means (already in place)

- Stack + recorder auto-start at login; caffeinate prevents mid-session
  sleep; the backend self-heals its own tick feed; the dead-feed watchdog
  can page a phone via ntfy.
- Nightly at 16:00 IST the evening report rebuilds the dataset and writes
  the scoreboard to `recorder/data/reports/` — unattended.
- **The one human step that cannot be automated is the daily Kite login**
  (30 seconds before 09:15). SEBI/broker rules make credential automation a
  non-starter; it stays manual by design.

## What "avoid wrong suggestions" honestly means

Not zero losing cards — a positive-expectancy system loses often and still
wins. The measurable version:

1. **Known failure modes stay caught**: hollow-volume cards, post-14:15
   entries, re-fired stopped strikes — each gate's catch-rate and saved-R
   reported monthly from the paper + hollow ledgers.
2. **No new failure mode goes unmeasured**: every card is paper-filled and
   graded, including vetoed ones, so a bad pattern shows up in data within
   days, not in painful hindsight.
3. **The filter must prove itself in shadow**: the probability model logs
   its opinion on every card for 4+ weeks before its number is even shown
   on a card. It never blocks a card and never trades.

## What "improve the scoring over time" means

The closed loop, monthly:

```
cards issued → paper-graded → dataset rebuilt → research run
(walk-forward, budgeted, holdout-locked) → evidence report
→ human decides → (optionally) owner adjusts weights/gates upstream
```

- Local side (this machine, sidecar-only): dataset, experiment harness,
  monthly gate-audit report. Nothing here touches the app's code.
- Owner's side (his call, his repo): any actual change to component weights
  or gates, made only when the evidence report shows a component that
  consistently fails to separate winners from losers over 100+ cards.

## Milestones

| When | Gate | Deliverable |
| --- | --- | --- |
| Week 1 (by 10-Aug) | keys in, first sessions | recorder capturing card births; first evening reports |
| Month 1 (by 01-Sep) | 30+ graded fills | baseline expectancy per mode, published in report |
| Month 2 (by 01-Oct) | 60+ fills, backfill done | first gate-audit: which gates saved R, which cost R |
| Month 3 (by 01-Nov) | 100+ graded cards | first research run (PROGRAM.md), holdout verdict, go/no-go on shadow filter |

## Hard boundaries (unchanged, ever)

- Advisory only — no order placement, no sizing automation, no live gating
  by the model. Signals reach this user only (SEBI RA/IA line).
- The paper ledger is the only scoreboard; a claim without a ledger number
  is an opinion.
- All local work stays out-of-tree; the owner's repo is pull-only.
