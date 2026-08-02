# Training guide — from recorded data to a probability model

The goal is **meta-labeling**: the rule engine keeps deciding *when* to fire a
card; the model learns to predict *"given this card and this market state,
what is the probability it wins?"* — and that probability is shown as context
(experiment-only: it informs, it never trades).

## The three learning loops (two already exist)

1. **Backtest replay** (in the app) — replays the regime/scoring engine over
   historical futures candles. Validates the *directional thesis* of the rules.
2. **Paper book** (in the app) — forward-fills every card with slippage and
   full charges. The only honest measure of *net* expectancy. This is the
   label source.
3. **Meta-model** (this guide) — trains on loops 1–2's output plus the
   recorder's market snapshots. Does not exist yet; needs data volume first.

## Data inventory

| Source | File | Role in training |
| --- | --- | --- |
| Signal archive | `backend/.signals_archive.jsonl` | one row per card: score components, regime, mode, strike, timestamps → **features + card identity** |
| Paper book | `backend/.paper_trades.json` | net-of-charges outcome per fill → **labels** (win/loss, R) |
| Hollow store | `backend/.hollow_signals.json` | vetoed-but-tracked cards → extra labeled samples |
| Recorder snapshots | `recorder/data/tradewell_history.db` (`snapshots`) | chain OI/PCR, pulse, indicators at card time → **market-state features** |
| Candles + backfill | same DB (`candles`, `candle_oi`) | trend/volatility context, years back via `backfill.py` |

## Prerequisites (do these now)

1. Kite keys in `backend/.env`, recorder launchd loaded — data accumulates.
2. After the first day's login, run the one-time history pull:
   `backend/.venv/bin/python recorder/backfill.py --days 365`
3. Wait. Honest sample-size gates, matching the repo's own philosophy:
   - **~30 graded fills** (2–4 weeks): compute baseline expectancy per mode
     from the paper book (`analyze.py`). No ML yet — this number is the bar
     any model must beat.
   - **~100 graded cards** (2–3 months): first model attempt is worth running.

## Training procedure (when the gate is met)

1. **Bench**: `backend/.venv/bin/pip install scikit-learn` (venv only — never
   into `requirements.txt`, that file is upstream's).
2. **Dataset build**: for each archived card, join:
   - card features: each score component's points (`score.components[]`),
     total, mode, direction, moneyness, hour-of-day IST, day-of-week,
     risk_reward, ref_spot vs invalidation distance;
   - market state: the card's `card_birth` snapshot (endpoint=`card_birth`,
     captured event-driven at issue time with the card id inside) — chain →
     PCR, ATM OI concentration, OI-change skew; pulse fields; indicators →
     RSI, ADX, VWAP distance, ATR; INDIAVIX daily close. Fallback for cards
     without one: nearest periodic snapshot *at or before* `created_at`;
   - label: the paper fill's net R (classification target: `net_R > 0`).
   Join on time with a tolerance (snapshots are 60s apart) and **only ever use
   data timestamped before the card** — leakage here silently inflates
   everything.
3. **Split — walk-forward only**: train on months 1..N, test on month N+1,
   roll forward. Never random/shuffled splits; time leakage is fatal with
   market data. Report every fold, not the best one.
4. **Models**: logistic regression first (calibrated, interpretable baseline),
   then `HistGradientBoostingClassifier`. With <500 samples anything deeper
   overfits; feature count should stay well under sample_count / 10.
5. **Metric that matters**: expectancy uplift, not accuracy. Filter test-fold
   cards at p ≥ 0.55 / 0.60 / 0.65 and compare **net R per trade and total R**
   of the filtered subset vs all cards. Also check Brier score for
   calibration and how many trades survive the filter (a filter that keeps 3
   trades a month proves nothing).
6. **Shadow deployment**: score each new card as it arrives, log
   `{card_id, p_win, model_version}` to `recorder/data/model_shadow.jsonl`,
   and *change nothing*. After 4+ weeks compare shadow-filtered vs actual
   outcomes. Only then consider showing the probability on the card — the
   human stays the decision-maker in this experiment.

## Improvement roadmap (rough order of value)

1. **NSE bhavcopy EOD backfill** — free official daily F&O archives give
   per-contract OI/volume history years back. Daily granularity only, but it
   puts real OI context behind backtests, which currently score technicals
   only.
2. **Long-window replay harness** — the app's backtest panel caps at 60 days;
   with backfilled candles the same logic can be replayed over years to
   re-validate every hand gate (14:15 cutoff, volume floor, refire guard) on
   large samples instead of one bad week each.
3. **Gate re-validation report** — automated monthly: for each gate, expectancy
   with vs without, from the paper + hollow books. Gates that stop earning
   their keep get flagged (evidence, not sentiment).
4. **Drift watch** — retrain monthly, compare feature importances and fold
   metrics over time; a model that decays is telling you the regime moved.
5. **More context sources** — FII/DII daily flows (NSE), event calendar
   (RBI/Fed/expiry days) as categorical features.
6. **Watch upstream Phase 6** — if the owner ships his own ML, ours stays a
   sidecar benchmark; comparable, never conflicting.

## The autoresearch pattern (karpathy/autoresearch, studied 02-Aug-2026)

Karpathy's repo has an agent autonomously iterate on a small GPT trainer:
edit `train.py` → fixed 5-min run → one ground-truth metric (val_bpb) →
keep/discard via git branch advance/reset → append every attempt to
`results.tsv` → repeat ~100× overnight. The human designs `program.md` (the
"research org"), never the code.

**Its subject matter does not apply here** — it pretrains a language model on
text with an NVIDIA GPU; our problem is a few hundred labeled tabular samples
on a Mac. **Its process absolutely applies** — as the harness for our
feature/gate/model search phase, with these adaptations:

- experiment = one feature-set / gate-parameter / model-config change;
  runtime is seconds on tabular data, so hundreds of experiments are cheap;
- metric = mean walk-forward OOS expectancy uplift, with a minimum
  trades-retained constraint (a filter keeping 3 trades proves nothing);
- keep the git-branch keep/discard mechanics, the append-only results log,
  and the simplicity criterion (a deletion that ties is a win) verbatim.

**The finance-specific danger — why we cannot copy it blindly:** val_bpb on
millions of held-out tokens generalizes, so metric-chasing is safe there.
Selecting the best of 100 experiments against a few hundred noisy market
samples is a multiple-comparisons machine — the "winner" is usually noise
(backtest overfitting). Mandatory armor for our version of `program.md`:

1. a **locked final holdout** (most recent month+) the loop never sees; only
   the single surviving candidate touches it, once, at the end;
2. walk-forward folds *inside* the loop — the metric is the mean across
   folds, never one split;
3. an **experiment budget** declared up front, every attempt logged, and a
   keep-threshold well above the noise band (not "any improvement");
4. ideas pre-listed per run — the agent works a queue, not an open-ended
   metric hunt.

**Status: BUILT (02-Aug-2026)** — see `experiment/`: `PROGRAM.md` is the
charter, `harness.py` the fixed evaluator (selftest proves the loop on
synthetic data), `experiment.py` the one agent-editable file, and
`dataset.py` produces `data/dataset.csv` + the locked holdout. The harness
refuses to run until the data gates are met (walk-forward needs 2+ usable
monthly folds), so it simply waits for the recorder + paper book to feed it.
Run a research loop only after the ~100-card gate; the mechanics are ready
today.

## Rules of the game (worth re-reading before every experiment)

- The baseline to beat is the rule engine's own expectancy, measured on the
  same out-of-sample window. "The model has 61% accuracy" means nothing.
- Small data: prefer fewer features and boring models; distrust any result
  that a one-week shift in the split destroys.
- Charges are part of every label (the paper book already nets them out).
- Never let the model place, size, or veto anything automatically. This
  project is an experiment; the model's job is to be *right about
  probabilities*, and the ledger's job is to prove it.
