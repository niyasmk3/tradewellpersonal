# PROGRAM — Tradewell research org (autoresearch pattern, quant armor)

You are an autonomous research agent iterating on ONE question: can a model
filter the signal engine's cards to a higher-expectancy subset? You edit
`experiment.py` only. Everything else is ground truth.

## Hard rules (violating any of these invalidates the whole run)

1. **Files**: you may edit `experiment.py` ONLY. Never `harness.py`,
   `dataset.py`, the dataset CSVs, or this file.
2. **The holdout is radioactive.** Never read `data/dataset_holdout.csv`,
   never run `--final` mid-run. It is evaluated ONCE, at the very end, and
   the result is accepted as-is — good or bad.
3. **Experiment budget: 25 per run.** Declared here so "just one more try"
   has a stop. When the budget is spent, stop and write the summary.
4. **Every attempt gets logged** (the harness appends to results.tsv
   automatically — never delete or edit that file).
5. **Sample gate**: if the harness aborts with "not enough data", the
   correct action is to STOP the run entirely and report that — not to
   loosen MIN_TRAIN/MIN_TEST (see rule 1).

## Setup (once per run)

1. Work in the `recorder/` git repo (it is its own repo, separate from the
   upstream app). Create a branch: `git checkout -b research/<date-tag>`.
2. Rebuild the dataset fresh: `../../backend/.venv/bin/python ../dataset.py`
   (paths from this directory; adjust from wherever you run).
3. Baseline first, always: run the harness on the UNMODIFIED experiment.py
   and log it. Every later idea is judged against this.

## The loop (repeat until budget is spent or the idea queue is empty)

1. Take the next idea from the queue below (or a better one you can justify
   in one sentence — write it into the queue first, then do it).
2. Edit `experiment.py`. One idea per experiment — never bundle.
3. `git commit -am "<idea>"`
4. Run: `../../backend/.venv/bin/python harness.py --run`
5. The harness prints a mechanical verdict:
   - **PASS** (mean uplift ≥ 0.05 R, positive in ≥ 2/3 folds, retention
     ≥ 40%, ≥ 30 test trades) → keep the commit, branch advances.
   - **FAIL** → `git reset --hard HEAD~1`. The attempt stays in results.tsv.
   - **Crash** → fix if trivial (typo), otherwise reset and move on.
6. **Simplicity criterion** (autoresearch's, kept verbatim in spirit): given
   equal metrics, fewer features and simpler models win. A PASS that adds
   ten features for +0.01 R over a simpler PASS is a discard. Removing
   features and staying equal is a keep.

## End of run

1. Pick the best surviving commit (highest mean uplift among keeps).
2. Run `harness.py --final` — once. Report both numbers side by side:
   walk-forward uplift and holdout uplift. If holdout collapses, say so
   plainly: the run found noise, and that is a finding.
3. Write a 5-line summary at the top of results.tsv's companion
   `NOTES.md`: what was tried, what survived, what the holdout said.

## Sequencing rule (added 18-Aug, after the owner's stopd pre-registration)

Decide the EXIT POLICY before training the model. The exit A/B reaches its
30-fill verdict first; if bank+cut is adopted, train against
`label_win_bankcut` (the deployed world), not `label_win` (the replaced
world). Never run the exit switch and the first model run in the same
week — confounded experiments teach nothing, twice.

## Pre-registered expectations (frozen 18-Aug, n=19 train — so September's
## run cannot quietly move the goalposts)

- Score total will NOT survive as a top feature (wins 83.2 vs losses 82.6).
- Candidates expected to matter, per current separations: vix_close (calm
  days win), pulse_range_pos_pct (enter low wins), atm_pe_ce_oi.
- If these three all flip sign at 40+, treat the 19-row separations as
  noise and say so in NOTES.md.

## Idea queue (work top-down; add, never delete)

- [ ] baseline as shipped (mandatory first run)
- [ ] baseline against label_win_bankcut — the deployed-policy label (compare
      to label_win baseline; adopt whichever matches the live exit policy)
- [ ] drop news + OI components (they're live-only — do technicals alone hold?)
- [ ] threshold sweep: 0.50 / 0.60 / 0.65 (one experiment each)
- [ ] HistGradientBoostingClassifier instead of logistic regression
- [ ] add pulse_* features
- [ ] interaction: score_total × hour_ist (late high-score cards — the 14:15
      cutoff hypothesis, now with data)
- [ ] per-mode models (intraday-only rows vs positional-only rows)
- [ ] calibration wrapper (CalibratedClassifierCV, isotonic) — does the
      probability mean what it says?
- [ ] feature pruning: keep top-8 by |coefficient| from the best model so far
- [ ] ENTRY-QUALITY framing (added 19-Aug from the R&D ledger): treat
      label_win_bankcut as the entry-quality target, not merely the exit-policy
      label — 17 of 32 clean fills never touched +5% and ALL 17 lost (−₹13.6k
      of the book's −₹12.2k; the touched 15 netted +₹1.4k with worst single
      −₹100 thanks to the de-risk rule). A filter that raises the +5%-touch
      rate attacks the dud pile directly; judge candidate filters by touch-rate
      retained as well as net-R uplift.
- [ ] tape feature: replicate upstream's tape classifier (developing / two-way
      / stretched) from OUR recorded candles as-of card birth — deterministic
      from prior price history, so retro-computable without leakage. The
      owner's sole surviving filter (GOLDEN stack) deserves a dataset column
      before the September run.
- [ ] premium-momentum-at-birth feature (19-Aug, idea from aaryansinha16/
      AI-trader's "option premium confirmation gate"): from recorder chain
      snapshots, the card contract's premium change over the 60-120s BEFORE
      birth. Three of our fills never traded a single tick positive — entered
      exactly at a local premium top; a falling-knife flag at birth is aimed
      straight at that failure mode. (Feature landed 19-Aug as
      premium_mom_pct — 20/21 train rows covered; the model run decides
      whether it earns a place.)
- [ ] thesis-level label (19-Aug, de Prado triple-barrier idea): label from
      the UNDERLYING's path — did spot touch the card's T1 level before its
      invalidation level within the window? — computed from recorded 3m bars.
      Separates "thesis right" from "premium paid" (IV crush and charges can
      fail a correct thesis); train entry filters on the thesis, grade P&L on
      the premium.
- [ ] pre-ignition hardening (de Prado purged-CV idea, 19-Aug): add a 1-day
      embargo at walk-forward fold boundaries in harness.py — a positional
      card opened at month-end can straddle the train/test line and leak its
      outcome across it. Tiny effect at monthly folds and hour-scale trades,
      but it is pure discipline and free. MUST land before the first real
      run, in a calm dedicated session, with the selftest re-run — harness.py
      is ground truth and does not get edited casually or late at night.
- [ ] DEFERRED to September, post-ignition (decided 27-Aug): a NIFTY-futures
      candle-rules lab cloning the gold pattern. Edge prior is LOW (NIFTY
      intraday is the most arbitraged tape in India — the 747-session
      climatology already shows ORB continuation at a coin-flip 52%), and
      the verdict queue is full. Revisit only if gold's forward book proves
      window-rules pay after charges. Climatology (knowledge only) shipped
      27-Aug instead: gaps ≥0.5% fill just 19% — NIFTY gaps run.
- [ ] GOLD H3 entry-timing fragility (03-Oct cross-check with the owner's
      independent implementation): the same rule reads −₹27k / +₹6k / +₹11k
      forward depending on a ONE-BAR entry convention at 18:00, because the
      entry sits on the US-release minute. Rule stays frozen; any fix is a
      pre-registered H4 ("enter at the first close ≥ 18:06, i.e. after the
      release candle" or "skip FOMC/CPI/NFP evenings"). Lesson for every
      future rule: never pin an entry to a scheduled-news minute, and write
      bar-time conventions (open- vs close-stamped) into the spec itself.
- [ ] tape-state instability (744-session climatology, 19-Aug): base rates at
      11:00 are two-way 35.8% / stretched 35.1% / developing 29.2%, but only
      47% of days keep their 11:00 state at 14:00 — and of days developing at
      11:00 only 30% still are at 14:00. The label is a MOMENT, not a
      day-type: model it as birth-time context (interaction with hour_ist),
      and never extrapolate a morning label across the afternoon.

## Ignition status (08-Oct-2026)

The data gates (40 train rows, 2 months) passed on 01-Oct and nightly_learn
has launched the baseline run every evening since — and every run ABORTED.
Two causes, one fixed:

1. **net_R was missing on half the rows (fixed 08-Oct).** The paper row's
   `stop_loss` is the FINAL stop; early-derisk and quick-target ratchet it to
   entry or above, so `(entry - stop) <= 0` and the R denominator vanished on
   66/126 fills. The harness saw 36 "usable" rows out of 70. dataset.py now
   recovers the stop the trade was BORN with (the fill-repriced SL in the
   `repriced` event, else the card's `premium_sl`). 70/70 rows carry net_R.
2. **Only 1 walk-forward fold exists; the harness requires 2 (unchanged).**
   The 28-day sealed holdout means the train set always lags a month: train
   = Aug (50) + early Sep (20) → one fold (train Aug, test Sep). The second
   fold arrives when October rows age into the train set, ~end of October /
   early November. This is the design working, not a bug — the fold rule is
   NOT loosened (rule 5). First legitimate 2-fold baseline run: ~early Nov.

Before that run, in a calm session: land the 1-day fold-boundary embargo
(queued below) and re-run --selftest. Nothing else changes.

## Hypotheses from the pro-trader interview (10-Oct-2026)

Transcript: recorder/notes/pro-trader-interview-2026-10-10.md. Measured
first, adopted as FEATURES or queued as pre-registered studies — never as
rule changes.

MEASURED (5y NIFTY, 1,267 sessions, bucketed by prev-close India VIX):
  <13: avg range 0.74%, big days (>1.5%) 3.0%  |  13-15: 0.98% / 11.9%
  15-18: 1.05% / 13.2%                        |  >18: 1.37% / 32.7%
  => the trader's "right market" claim is real on our tape: big-move days
  are ~11x more frequent above VIX 18 than below 13.
  CONTEXT (corrected the same evening — see DATA INTEGRITY below): of the
  130 graded cards, 95 were born below VIX 13 (the trader's "don't buy"
  zone), 28 at 13-15 and 7 above 15 (all in October, when VIX climbed to
  15.2). The paper book's verdict is mostly a low-VIX verdict; the engine
  above 15 is barely measured. Model conclusions must carry the VIX mix of
  their sample, and the vix_close x score interaction stays queued until
  the >15 bucket reaches 30 cards.
  DATA INTEGRITY (10-Oct): the deep-backfilled dailies (spot, futures,
  INDIAVIX) had stopped on 19-Aug because backfill.py needs a login-day
  Kite token, so vix_close and htf_ret_20d_pct were silently stale for
  every later card (the first draft of this note wrongly said "every card
  at VIX 11-12"). Fixes: daily_extend.py (nightly, before dataset.py:
  derives dailies from the live 3m feed and the Closing lab's 5-minute VIX
  store, provenance in derived_daily; an official backfill.py run on a
  login day overwrites them); vix_close is now strictly the PREVIOUS
  session's close (the old lookup matched the same day's midnight-stamped
  bar = look-ahead for morning cards); new vix_at_birth feature (exact
  5-minute value at the card's birth).
  Expiry day (Tue) on our 121 cards: 42.9% win (best weekday) but net
  -Rs3.5k; Thu worst (20%, -Rs17.5k) — n=21-34 per day, hints only.

ADDED AS FEATURES (10-Oct): dte_at_entry, is_expiry_day, event_flag
(scheduled-event warning on the card), holds_overnight (late-day
positional gap warning), htf_ret_20d_pct (daily-structure bias).
vix_close was already present (definition fixed, see DATA INTEGRITY);
vix_at_birth added. NOTE: event_flag has fired on 0 of 136
cards so far — no scheduled event fell inside a card window Aug-Oct — so
the trader's event rule stays unmeasured until one does.

- [ ] VIX x card interaction: does score quality differ by vix_close band?
      (only testable once >13 cards exist — note the date it first happens)
- [ ] "blast exit" counterfactual: exit on the first 60s chain snapshot
      where the premium gains >=X% in one step after entry, vs the ratchet;
      pre-register X from the climatology (needs the premium path, which the
      60s snapshots provide). Trader's claim: the spike IS the exit.
- [ ] time-stop at resistance: positions consolidating 90-120 min without a
      new high -> exit; needs the Patterns module's S/R levels joined to
      fills. Queue until levels are in the dataset.
- [ ] scale-out at HTF resistance / add-back on sustained breakout: a
      PAIRED-twin study (needs multi-lot twins — out of scope until R3
      policy ledgers mature).
- [ ] sizing policy comparison for the HUMAN book: "full premium at risk"
      (TRADING_FUND = daily risk budget, lots = fund / premium) vs the
      current risk-vs-disaster-stop sizing. A user decision, pre-register
      the comparison on the live book before changing .env.
- [ ] overtrading early-warning (orders/day, charges/day above trailing
      median) -> a line on the research page from the tradebook import; a
      Telegram nudge only if the live book shows it predicts bad days.

## Promotion-to-LIVE bar (added 21-Aug after an external Codex review)

The harness KEEP rule promotes ideas between RESEARCH branches only. For any
policy to influence real money, the bar is far higher and is written down
here before anyone is tempted to lower it in the moment:

- 100+ independent forward fills under the candidate policy;
- positive net expectancy after ALL charges;
- clustered-by-day 95% confidence interval above zero;
- profit factor > 1.2;
- no single day contributing more than 25% of total profit;
- positive in 2+ distinct regimes / rolling windows;
- the permanent results.tsv trail of every variant ever tried.

Nothing in this repo promotes automatically. A 30-sample verdict is an early
KILL checkpoint, never proof of profitability.

## Why these rules exist (read once, believe forever)

100 keep/discard iterations against a few hundred noisy samples is a
multiple-comparisons machine; the "best" config is usually a lucky one.
The walk-forward mean, the retention floor, the budget, the one-shot
holdout, and the append-only log are what make a surviving result mean
something. autoresearch can skip all this because val_bpb on millions of
held-out tokens generalizes; net_R on ~300 trades does not.
