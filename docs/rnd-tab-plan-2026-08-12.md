# R&D Tab — Plan (12-Aug-2026)

**Goal (user):** a Research & Development tab that automatically tracks signals and
suggests improvements, oriented around one question: *does a signal deliver ≥5%
premium profit after entry, inside its mode's time window?*

| Mode | Target | Window |
|---|---|---|
| scalp | +5% | 30 min from entry |
| intraday | +5% | 120 min from entry |
| positional | +5% | 240 min from entry, capped at **14:50 IST** same day |

Analysis buckets: **3–5% · 5–10% · 10–20% · >20%** (plus the loss-side mirror).

**Honesty box — what today's evidence already says.** 71 honest-era clean paper
fills (33 intraday / 25 scalp / 13 positional), 67 with from-entry excursion
tracking. 52% touch +5% at *some* point; median MFE 5.3%; median time-to-peak
**3 minutes** (peaks are early — consistent with the instant-reversal failure
taxonomy). The 30-Jul audit found +5% ≈ breakeven expectancy and no positive
level above it; the +5% early-derisk ledger exists precisely to price this
trade-off. So the tab's job is NOT to assume 5% is achievable — it is to
measure P(+5% in window) per condition, find where that probability is highest,
and price the exit policies that would harvest it. A "min 5% guaranteed" does
not exist; a "which conditions give 70%+ odds of 5%-in-window" table can.

---

## R1 — Instrumentation first: the touch ladder (the data that can't be rebuilt)

Today we store `mfe_premium`/`mfe_at` (magnitude + time of the MAX). A trade
that touched +5% at minute 20 and peaked +12% at minute 200 is invisible to a
window analysis. Fix at the source:

- `Trade.touch_times: dict[str, int]` — first-touch epoch per level:
  `{"+3": ts, "+5": ts, "+10": ts, "+20": ts, "-3": ts, "-5": ts, "-10": ts, "-20": ts}`.
  Levels FROZEN in code (`monitor.TOUCH_LEVELS_PCT`), not a config knob —
  changing levels mid-stream resets cross-trade comparability (the
  setup-detector parameter rule). [Amended at R1 build: was a config knob.]
- Recorded inside `trades/monitor.evaluate()` at the excursion step — one hook
  covers BOTH the live journal and the paper book (they share `evaluate`).
  5s monitor cadence ⇒ ±5s precision, fine at these horizons.
- Persisted by the existing stores; append-only semantics (a level's first
  touch never changes). Tests pin: first-touch latch, no overwrite, both signs.
- **Historical fills cannot be backfilled** — pre-ladder rows are analyzed with
  the `mfe_at` approximation and permanently flagged `approx` in every table.
  (Same lesson as IV/OI logging and condor snapshots: log first, argue later.)

## R2 — The `/rnd` tab (read-only analytics over existing stores)

Backend `app/rnd/analytics.py` + `api/routes_rnd.py` (`GET /rnd/summary`,
`/rnd/matrix?dim=`, `/rnd/policies`), joining the paper book + signal archive +
eval trace (all mtime-cache-keyed like `signals/archive.py` — no DB). Frontend
`/rnd` module tab via the established 3-file pattern.

Sections:

1. **Headline KPI strip** — per mode: `P(≥5% within window)`, median
   time-to-5%, n, and the same for each bucket (3–5 / 5–10 / 10–20 / >20).
   Every figure carries its n; anything under `RND_MIN_SAMPLE (30)` renders
   grey with "accumulating (n=x/30)" — never a green number on thin data.
2. **Reach matrix** — bucket × window curves (what % reached each bucket by
   +15/30/60/120/240 min), per mode. Exact for ladder-era rows, `approx`
   badge for older.
3. **Conditioning cuts** — the reach KPI sliced by the dimensions the archive
   already stamps: score band, regime, tape state, golden, entry hour, DTE,
   volume/OI component bands, day-of-week. One dimension at a time (the
   MODEL-D lesson: stacked cuts on this n select noise).
4. **Cost-of-capture panel** — for trades that DID reach +5% in-window: MAE
   before the touch (how much heat the 5% harvest requires), and for those
   that didn't: where they died. Reuses `trades/excursion.py` machinery.
5. **Signal-to-outcome ledger** — every archived card joined to its paper
   outcome, filterable, with the touch ladder rendered as a mini timeline.

## R3 — "Suggest improvements", the house way (no auto-tuning)

Two engines, both deterministic:

1. **Pre-registered policy counterfactuals** (the actual suggestion engine).
   Candidate exit policies run as PAIRED shadow ledgers against the live
   policy, exactly like exit_ab/stopb/stopc:
   - `P-5W`: book at +5% inside the window, else exit at window end.
   - `P-5T`: book at +5% inside the window, else hold to normal exits (isolates
     the window-exit leg).
   - `P-LADDER`: book ½ at +5%, trail the rest (vs the current ratchet).
   Each pre-registered here (thresholds frozen at the windows/buckets above),
   graded net of charges, verdict at **30+ diverged pairs** per mode, human
   flips any knob — never the code.
   [Amended at R3 build: P-LADDER needs ≥2 lots to physically halve, so it
   books at 2 lots and is paired against a dedicated 2-lot standard-policy
   twin (`rnd-base2`) — identical sizing on both arms — instead of the 1-lot
   clean fill. Standard stops keep applying to every twin: a policy that
   ignores stops isn't comparable to anything we'd trade. Twins enabled via
   RND_POLICY_LEDGERS in .env (code default false — the paper suite's
   row-count expectations predate them).] The tab shows each policy's ledger state:
   `accumulating (n=x/30)` → `VERDICT: beats/loses to baseline by ₹X/trade`.
2. **Deterministic insight scanner** — a fixed rule sweep over the R2 matrices
   that surfaces CANDIDATE hypotheses only when a cell clears: n ≥ 30, gap vs
   complement ≥ +0.5% premium expectancy, and the sign holds on a frozen
   DEV/TEST date split. [Registered at R3 build: split = 2026-08-04 00:00 IST;
   metric = gross realized premium move % per trade; changing any frozen
   parameter resets every candidate's standing.] Output is explicitly labelled "candidate — needs its
   own pre-registered ledger before anything acts on it". No LLM in this path;
   optionally the existing assistant (Haiku) summarizes the tables into prose,
   display-only, same rules as the dashboard chat.

## R4 — Acting on verdicts

A cleared verdict becomes a one-line config change the user makes (e.g. quick
target to 5% for scalp, or a time-boxed exit), each with its own follow-up
ledger. The tab never edits config.

## Config (`RND_*`, .env)

```
RND_WINDOW_SCALP_MIN=30      RND_WINDOW_INTRADAY_MIN=120
RND_WINDOW_POSITIONAL_MIN=240  RND_POSITIONAL_CUTOFF_IST=14:50
RND_MIN_SAMPLE=30            # touch levels frozen in monitor.TOUCH_LEVELS_PCT (R1 amendment)
```

## Files

New: `app/rnd/{__init__,analytics,policies}.py`, `api/routes_rnd.py`,
`tests/test_rnd_*.py`, frontend `app/rnd/page.tsx` + `components/RndLab.tsx`.
Modified (small): `trades/models.py` (+`touch_times`), `trades/monitor.py`
(ladder hook), `paper/service.py` (policy twins, per the stopb pattern),
`config.py`, `main.py`, `ModuleSwitcher.tsx`, `lib/api.ts`.

## Sequencing & gates

1. **R1 ships alone first** (pure logging, zero behavior change) — every day
   it isn't live is a day of unreconstructable window data lost.
2. R2 ships next on whatever data exists (approx-flagged history + exact new).
3. R3 policies pre-registered at R2 ship time, ledgers start immediately;
   first verdicts realistically 3–6 weeks away at current fill rates
   (~2-3 clean fills/day ⇒ 30 diverged pairs per mode takes a while —
   positional (13 fills in 3 weeks) will be the slowest read).
4. Adversarial review before commit, as always.

**Not in scope:** any automatic parameter change; any LLM-derived trade rule;
re-litigating rejected filters (range-spent, day-alignment, analog-day) without
a new evidence class.
