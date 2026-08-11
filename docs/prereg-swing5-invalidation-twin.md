# Pre-registration — Swing-5 Invalidation Twin ("stopd")

**Registered:** 10-Aug-2026, BEFORE any implementation or data collection.
**Status:** REGISTERED, NOT LAUNCHED. Launch trigger below.

## Hypothesis

Widening the underlying-invalidation lookback from the last **3** candles to the last
**5** candles reduces noise stop-outs enough to improve net expectancy on real premiums.

## Evidence basis (what motivated this — all proxy, theta-blind)

- Stop-design sweep 10-Aug (25 policies, 52 sessions, 469 episodes, DEV/TEST split,
  results in scratchpad `stop_sweep_results.json`, summarized in the 10-Aug methodology
  audit): `swing5/t1.5` was one of only two policies beating the current bracket in
  **all four** arm×period cells (+0.03 to +0.16R per cell; only positive cell in the
  sweep: confirm-gated DEV +0.022R at 44% WR).
- Failure taxonomy: stop-too-tight class = 31% of live loss rupees; 65% of +5%-touch
  winners historically dipped through a −2.5% premium band first.
- Missed-move analysis: captured major moves lose −1.05R median under the 3-bar stop.

## Frozen specification (no tuning after registration)

- **Twin:** for every CLEAN intraday paper fill, book one shadow twin identical in every
  respect except `invalidation_level`, recomputed at issue from the **min low of the last
  5 closed candles** (CE; mirror with max high for PE), same VWAP-tightening rule as live
  if and only if it applies under the same conditions as the 3-bar version.
- Shadow class tag: `hollow: stopd:` — capacity-exempt, 1:1 with clean fills, never
  offered live, excluded from all clean aggregates (same discipline as stopb/stopc).
- **Pairing:** by signal_id against the clean fill. Only DIVERGED pairs carry information.
- **Verdict bar:** 30+ diverged pairs. Twin must beat the clean arm on net expectancy
  (charges included). Positive → promote SWING lookback 3→5 for the live invalidation
  (human decision, config change); negative → registration closes, parameter stays 3.
- **No parameter sweeps post-launch.** 5 is the registered value; any other value is a
  new registration.

## Launch trigger

Launch ONLY after **stopb (stop-basis A/B) or stopc (stop-calibration A/B) reaches its
30-diverged-pair verdict** — three concurrent stop experiments on the same fill stream
would confound each other's reads. Until then: no code.

## Known risks / confounds (stated now)

- Proxy evidence is theta-blind and pre-charges; the live twin is the only truth.
- A wider stop enlarges per-trade loss size; expectancy, not win rate, is the bar.
- Interaction with STOP_PRIMARY=underlying: the twin moves the SAME level the live exit
  monitor acts on, so divergence will be frequent (good — information-rich pairs).
- Regime dependence: sweep TEST period was regime-hostile; all cells were still negative
  in absolute terms. This twin claims *improvement*, not profitability by itself.
