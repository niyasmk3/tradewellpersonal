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
   Ops note (14-Oct-2026): the app checkout sits on local branch `local-ui`
   (21-line dashboard patch: BANKNIFTY/FINNIFTY cards, which the owner's
   NIFTY-only panel hides); `main` stays pristine — routine in ops/README.md.
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
- [x] "blast exit" counterfactual: exit on the first 60s chain snapshot
      where the premium gains >=X% in one step after entry, vs the ratchet;
      pre-register X from the climatology (needs the premium path, which the
      60s snapshots provide). Trader's claim: the spike IS the exit.
      -> RUNNING AS TWINS since 10-Oct-2026 (recorder/exit_twins.py, nightly;
      X frozen at 5% — spec block "FROZEN SPECS — exit twins" below).
- [x] time-stop at resistance: positions consolidating 90-120 min without a
      new high -> exit; needs the Patterns module's S/R levels joined to
      fills. Queue until levels are in the dataset.
      -> RUNNING AS TWINS since 10-Oct-2026 in its premium-path form (90 min
      without a new premium high, intraday + positional; spec block below).
      -> 11-Oct-2026: monthly-expiry positional contracts (invisible to the
      recorder's front-week chain) now graded from the condor module's
      5-minute chain store — time-stop ONLY, pre-registered in the spec
      block "Second path source" before its first run.
      The S/R-conditioned variant stays queued until Patterns levels are
      joined to fills.
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

### FROZEN SPECS — exit twins (frozen 10-Oct-2026)

Code: recorder/exit_twins.py, nightly right after dataset.py in
evening_report.sh. Output: recorder/data/exit_twins.json + the research-page
section "Exit twins (interview hypotheses, pre-registered 10-Oct)".

Paired 1:1 with every clean paper fill: same entry, contract, size and
charges; only the exit differs. The twin's exit is replayed on the
recorder's 60-second chain snapshots (contract matched by instrument
token) and the book's REAL exit caps the twin's life — whichever comes
first. Verdict text is literally "pending — N/30 diverged pairs" until 30
diverged pairs exist for that policy AND mode; a human reads the result,
nothing is applied. Changing any parameter below resets the verdict clock.

- Policy A "blast exit" — X = 5%, frozen 10-Oct-2026. Exit at the first
  snapshot after entry whose premium is >= 5% above the PREVIOUS sample
  (one-minute step, same session day), else the book's exit. The FIRST
  step is measured from the entry fill — the premium the buyer actually
  holds — later steps snapshot-to-snapshot, so a spike that happened before
  the fill is never credited to the twin. X is the 95th percentile of
  |one-minute premium moves| (snapshot-to-snapshot) over all gradeable paths
  on 10-Oct-2026 (p95 = 5.412% over 836 unique steps; p50 1.6%, p90 4.2%,
  p99 9.4%), rounded to a whole percent — printed by `exit_twins.py
  --calibrate`, which refuses to compute a twin outcome. All three modes.
  AUDIT TRAIL (10-Oct): the first pass anchored each path at the last
  snapshot BEFORE entry and read p95 = 5.924% -> 6%; its first run showed a
  twin "blasting" seconds after entry at a price BELOW its own fill — the
  jump had happened before the entry (cards fire after spikes). The path
  definition was corrected the same day (coverage from entry, first step
  from the fill), the rule re-applied mechanically gave 5%, and the clock
  restarted at zero — no verdict had been reached and nothing was adopted.
- Policy B "time-stop" — 90 minutes, frozen 10-Oct-2026 (the lower edge of
  the trader's 1.5-2h); intraday and positional only (scalps hold ~4 min,
  the rule is vacuous there). The premium's high-water mark is seeded at
  the entry fill; a new high is a snapshot strictly above the mark. Exit at
  the first snapshot >= 90 IN-SESSION minutes after the last new high, else
  the book's exit. A trade that never exceeds its entry therefore exits at
  entry + 90 session-minutes.
- Gradeable path: the contract present in the recorded chain and no
  in-session gap > 3 minutes anywhere from entry to the book's exit (entry
  -> first snapshot, consecutive snapshots, last snapshot -> exit; session
  = 09:15-15:30 IST on days the recorder saw candles or snapshots). Not
  gradeable, reason counted: contract not in the recorded chain (the
  recorder stores the FRONT-WEEK chain, so monthly-expiry positional
  contracts have no path — 31 of 135 fills on 10-Oct; the condor module's
  chain store has the monthlies at 5-minute cadence — pre-registered on
  11-Oct as the SECOND PATH SOURCE for Policy B only, sub-block below),
  quarantined fills (off-session exit / journal override), gaps.
- Charges: BOTH arms netted identically — (fill - entry) x qty minus the
  paper book's Zerodha round-trip schedule (backend/app/paper/charges.py,
  imported read-only; the function the upstream R&D policy twins use in
  rnd/policies.py::_net). Twin fill = snapshot LTP x (1 - 0.4%), 2dp, min
  Rs0.05 — the book's own simulated exit-fill rule. Paired delta = twin
  net - real net per trade; R = delta / ((entry - initial stop) x qty) via
  dataset.initial_stop(). NOTE (found 10-Oct): the book's stored
  realized_pnl — dataset.py's net_pnl label — equals (exit - entry) x qty
  on every clean fill: slippage-adjusted but GROSS of charges. The twins
  re-net both arms, so their delta is charge-consistent.
- Divergence = the twin exited on its own trigger before the book's exit.
  The JSON also carries the upstream economic count (|delta| > Rs1).

#### Second path source — the condor module's 5-minute chain store (pre-registered 11-Oct-2026, written before its first run)

Why: 31 of the 135 clean fills on 10-Oct were positional contracts on a
MONTHLY expiry, invisible to the recorder's front-week 60-second chain —
exactly the fills the time-stop is meant for (its positional column had
zero diverged pairs). The condor module keeps its own chain snapshots in
backend/.condor_chain.db (table chain_snap: ts, token, tick_ts, symbol,
expiry, strike, right, ltp, bid, ask) and that store carries the monthly
expiries at a 5-MINUTE cadence from 20-Aug-2026 (measured 11-Oct before
any outcome: 2,240 of 2,303 same-day snapshot steps are 300 s; the rest
are a few 60 s steps and restart gaps). It is opened with sqlite
`mode=ro`; nothing under backend/ is ever written.

Rules, frozen before the first outcome was computed:
- Trigger: the fallback is tried ONLY when the 60-second chain holds no
  row for the contract's instrument token anywhere in the holding window
  (entry - 10 min -> the book's exit) — whether because the recorder stores
  a different expiry or because it recorded nothing in the window. A
  contract that IS in the 60-second chain but fails the 3-minute gap rule
  stays "not gradeable": the source is decided by the contract's presence,
  never by the outcome. Tried only for fills in the time-stop's modes
  (intraday, positional); a scalp without a 60-second path stays not
  gradeable, since no policy could use its 5-minute path.
- Matching by instrument token, like the primary source. Sample time =
  the snapshot wall time `ts` (the same convention as the 60-second path);
  LTP as stored, rows with LTP <= 0 are missing; bid/ask are ignored — the
  twin fill stays LTP x (1 - 0.4%), the book's own rule.
- Gap rule: the SAME in-session gap logic (entry -> first sample,
  consecutive samples, last sample -> exit; session 09:15-15:30 IST on the
  recorder's trading-day calendar, which contains every day the condor
  store saw) with a 10-MINUTE threshold instead of 3. Fills entered before
  20-Aug-2026 have no 5-minute snapshots and stay not gradeable; so does a
  hold that straddles a condor outage.
- Policy B (time-stop) is the ONLY policy that uses a 5-minute path. Its
  rule is unchanged (90 in-session minutes after the last new premium
  high, mark seeded at the entry fill); at 5-minute resolution a new high
  is only seen at a 5-minute sample and the trigger lands at the first
  sample past the deadline, so the twin's exit can come up to ~5 minutes
  later than it would on a 60-second path. Read the positional column
  knowing it mixes the two cadences; the split is printed beside it.
- Policy A (blast) is NOT evaluated on a 5-minute path: the one-minute
  jump rule cannot be evaluated at 5-minute cadence — a 5-minute move is a
  different quantity whose distribution was never calibrated. Those fills
  are counted under Policy A as not gradeable with the reason "5-minute
  cadence (time-stop only)". The --calibrate distribution (|one-minute
  move|, the frozen X = 5%) is computed over 60-second paths only.
- Reporting: every pair carries "path_source": "chain_60s" | "condor_5m";
  the per-mode summaries show the split by source. The verdict bar is
  unchanged — literally "pending — N/30 diverged pairs" per policy AND
  mode, pooled across the two sources.
- Clocks: Policy A and every 60-second path keep their 10-Oct spec and
  clock untouched. Policy B's clock is not reset either — no parameter of
  the rule changed and no existing pair is re-graded; the second source
  only adds pairs that had no path before (0 positional diverged pairs
  existed on 10-Oct).

## DATA INTEGRITY (13-Oct-2026) — three pre-registered corrections to the ground truth

Written on the evening of 12-Oct-2026 BEFORE the rebuild; every count in
[square brackets] was filled in from the rebuild that followed and nothing
else in the text was changed. First nightly run with the corrections:
13-Oct. The holdout stays locked — the before/after counts are a mechanical
cell diff of the regenerated CSVs (both files, keyed by card_id); no model
was fitted and no feature-to-label relationship was read. Nothing here
changes a feature's definition except premium_mom_pct's contract match, and
no experiment has completed on either CSV yet (every nightly baseline since
01-Oct aborted at the 2-fold gate; results.tsv does not exist; ignition is
early November), so no verdict is affected. All three defects were first
seen while building the exit twins (10/11-Oct notes) and were verified again
today against the raw stores before any code changed.

1. LABELS WERE GROSS OF CHARGES. The book's stored realized_pnl equals
   (exit - entry) x quantity on 483 of 484 closed paper rows (the exception
   is a half-exit R&D twin) and on all 9 closed live-book rows: slippage-
   adjusted but gross of Zerodha charges — while dataset.py's docstring
   claimed "charges-net realized P&L -> label". Correction: label_fields()
   nets the stored P&L with the book's own schedule, charges(entry, exit,
   quantity, legs=2) from backend/app/paper/charges.py imported READ-ONLY by
   file path (the exit twins' convention; a frozen mirror of the schedule is
   the fallback if the file ever moves, cross-checked on a worked example at
   import). label_win and net_R (via initial_stop) now derive from the
   netted value; net_pnl in the CSV IS the netted value; a new `charges`
   column makes the netting auditable per row (an outcome column, never a
   feature). A fill without entry/exit/quantity has no charges-net label and
   is skipped rather than labelled gross (none today). Expected effect:
   small — charges average ~Rs 70 per round trip, so only fills that won by
   less than their own charges flip from win to loss; net_R shrinks by the
   charges over the initial risk on every row.
   RESULT (rebuild, evening of 12-Oct): label_win flipped on 4 of the 129
   rows present on both sides — three intraday fills and one scalp that had
   won Rs 19.50-39.00 gross and lose Rs 12-42 net (NIFTY-intraday-
   1785823741-PE, -1789457580-PE, -1790661784-CE, NIFTY-scalp-1788928321-PE);
   win rate 27.9% -> 24.8% (32/129). Charges total Rs 8,838.45 over the 129
   dataset rows (mean Rs 68.52, range 49.36-108.28) and Rs 9,766.67 over all
   139 clean closed fills. net_pnl over the same 129 rows: -Rs 32,769.15
   gross -> -Rs 41,607.60 net; the identity net = gross - charges holds on
   every row to the paisa. net_R mean -0.144 -> -0.226; per-row change mean
   -0.082 R (median -0.079, range -0.297 to -0.013). The old 135-row file's
   sum of -Rs 17,430.70 had been flattered by the five restart exits
   (+Rs 15,592.85 between them, item 3).

2. PREMIUM MOMENTUM READ THE WRONG CONTRACT ON MONTHLY POSITIONALS.
   premium_momentum() matched chain rows by STRIKE only. The recorder stores
   the FRONT-WEEK chain, so for a positional card on a monthly expiry it read
   the weekly contract at that strike: 31 of the 135 graded cards were born
   with a card expiry different from the front-week chain's (all
   positional; 8 positionals sat on the front week). Correction: rows are
   matched by the card's instrument token against the row's ce_token /
   pe_token (every card and every chain snapshot since 03-Aug carries
   tokens). The fallback is strike + IDENTICAL chain expiry, used only when
   the card has no token or a snapshot's rows carry none; strike-only
   matching is gone. A monthly contract absent from the front-week chain now
   has NO premium_mom_pct (the harness imputes the median) instead of a
   wrong one — a one-to-two-minute momentum cannot be read from the condor
   store's 5-minute cadence, and mixing cadences inside one column is the
   mistake the twins' 11-Oct spec refused. Expected effect: weekly rows
   unchanged to the last digit; the monthly positional rows lose their
   value; no row gains one.
   RESULT: 23 of the 129 common rows changed — every one a monthly
   positional and every one from a number to missing (value -> missing 23,
   missing -> value 0, number -> number 0, so a median |change| between two
   numbers does not exist); the other 3 monthly positional rows were already
   missing; all 103 weekly rows are unchanged to the last digit. Coverage
   125/129 -> 102/129 (102 matched by token, 0 by the strike+expiry
   fallback). The 23 discarded wrong-contract values had median +0.31%
   (median magnitude 2.52 points, range -8.76% to +25.97%) — a weekly
   contract's one-minute move stamped on a monthly card.

3. EXITS STAMPED AT A BACKEND RESTART. The paper monitor closes positions
   on the first quote after an outage without marking them: during the
   outage nothing runs, and on the first cycle back the stop / target /
   invalidation that was crossed meanwhile fires and is priced at that
   first tick — nothing like the level in the written rules. Five clean
   positional exits sit at the exact minute the backend came back, verified
   per contract in both stores: T-7bd24304c2 (NIFTY 24100 CE, 31-Aug
   11:58:44 — the condor store's first snapshot of the day is 11:58:44, the
   recorder's first chain snapshot 11:59:26); T-0a2fff2989 and T-50f8df1e98
   (NIFTY 22650 PE / 22700 PE, both 01-Oct 13:49:22 — first snapshots
   13:49:22 / 13:50:26, a 4.5-hour gap from the open); T-7062c51313 and
   T-c5d9be1e26 (NIFTY 22500 PE / 22400 PE, both 09-Oct 10:39:10 — first
   snapshots 10:39:10 / 10:39:23). The two 01-Oct "target1" exits are the
   book's two largest wins (+Rs 13,795 and +Rs 12,856), i.e. the whole
   outage move booked as if the target had filled. Correction, mechanical:
   exit_after_feed_gap(trade) is True when the exit stamp lies within 2
   minutes of the first same-day quote of the contract that ends a gap
   longer than 10 minutes in that contract's own snapshots. Source order:
   the recorder's 60-second chain first (the token's row with LTP > 0), the
   condor 5-minute store (backend/.condor_chain.db, sqlite mode=ro, never
   written) only when the 60-second chain holds no row for the token that
   day. The gap runs from the previous same-day sample (clamped to the 09:15
   open — pre-open frozen snapshots are not quotes) or from 09:15 when there
   is none; a gap that ends inside the session's first quote window
   09:15-09:20 is the legitimate overnight open and never counts. "Within 2
   minutes" is symmetric because the recorder polls the backend once a
   minute, so its first post-gap snapshot trails the backend's own first
   quote (13-66 s in the five cases; the condor store, written by the
   backend itself, lands on the exit second). Quarantine, the same way the
   off-session exits are quarantined: the five ids go into
   data/journal_overrides.json with reason "exit stamped at backend
   restart", and the flag joins the off-session gate in outcome_by_signal(),
   the research page's quarantine count (shown beside the existing note) and
   the exit twins' quarantine line, so new cases are caught nightly without
   a human. The nightly log names every flagged fill and warns when a
   contract-level gap is NOT a symbol-wide gap (a strike that left the
   recorded window — the rule's one known false-positive mode; 0 such cases
   today, token-level and symbol-level agree on all 139 clean closed fills).
   Fills whose contract has no same-day snapshot in either store cannot be
   checked and stay as they are: 9 today, all positionals on the 25-Aug
   monthly exited 05-18 Aug, before the condor store began on 20-Aug (one of
   them is the already-quarantined 17-Aug off-session exit). Owner note:
   notes/owner-note-2026-10-13-restart-catchup-exits.md.
   RESULT: flagged 5 of 139 clean closed fills — exactly the five, all via
   the condor store (the 60-second chain has no row for a monthly token),
   gaps 163 / 274 / 274 / 84 / 84 min from the 09:15 open, exit on the first
   quote to the second; the symbol-wide check agrees on all five; 9
   unverifiable, as predicted. Research page: 10 quarantined fills, of which
   5 restart catch-up exits. Dataset 135 rows (train 77 / holdout 58) -> 129
   (76 / 53): the five plus the hollow-labelled row below. Exit twins
   unchanged pair for pair (103 blast / 70 time-stop pairs, same diverged
   counts); only their not-gradeable reasons moved (quarantined 5 -> 10,
   "gap > 10 min in 5-min path" 6 -> 1).

   Plumbing found on the way and fixed in the same step because the
   quarantine is unsound without it: outcome_by_signal() took the FIRST
   closed trade per signal_id and never excluded hollow rows, so an R&D
   policy twin (notes "hollow: rnd-...", twins since 09-Oct share the real
   fill's signal_id) becomes the card's label whenever the real fill is
   still open or quarantined. Today that is 1 row — the FINNIFTY 24850 PE
   positional of 12-Oct, whose real fill has not closed while its +5%-window
   twin has. Hollow rows never label a card now; the card re-enters the
   dataset when its real fill closes.
   RESULT: 1 row removed (that FINNIFTY card).

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
