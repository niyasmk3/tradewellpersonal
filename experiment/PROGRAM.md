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

## Idea queue (work top-down; add, never delete)

- [ ] baseline as shipped (mandatory first run)
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

## Why these rules exist (read once, believe forever)

100 keep/discard iterations against a few hundred noisy samples is a
multiple-comparisons machine; the "best" config is usually a lucky one.
The walk-forward mean, the retention floor, the budget, the one-shot
holdout, and the append-only log are what make a surviving result mean
something. autoresearch can skip all this because val_bpb on millions of
held-out tokens generalizes; net_R on ~300 trades does not.
