# P1-4 Setup Detectors — Plan (03-Aug-2026)

The audit's P1-4: "first two setup detectors (breakout, VWAP reclaim),
shadow-only → paper." The cadence replay (P1-3) sharpened the why: the
capture ceiling is DETECTION — ~78% of mechanical moves have no gate-78
candidate under any throttle policy. Detectors are the recall play, and the
audit's own rule applies: sized offline first, shipped behind the paper book,
promoted only by fills.

## Step 0 — sizing replay (DONE, this doc's evidence)

Same 60d/3m futures dataset, same 200-event move universe, same conservative
outcome model as the P1-3 replay. Detector parameters fixed **a priori**
(classical definitions, round numbers) — deliberately not swept against
outcomes; a detector that needs fitting to look good has already failed.

| Candidate stream | n (41 sessions) | /day | WR | Exp (R) | Capture | New events vs engine |
|---|---|---|---|---|---|---|
| Engine gate-78 (all slot-free candidates) | 680 | 16.6 | ~38% | −0.15..−0.22 | 21.0% | — |
| **VWAP cross** (reclaim + reject, both dirs) | **51** | **1.24** | **47.1%** | **+0.039** | 7.0% | **+9 events (+4.5pp)** |
| Breakout (compressed hour → range break) | 16 | 0.39 | 25.0% | −0.428 | 1.0% | +1 event |

Read honestly:

- **VWAP cross is the first candidate stream this project has measured with
  non-negative expectancy on the underlying.** 47% WR at 1.5R (breakeven is
  ~40%) across 51 signals, and 9 of its captured moves are invisible to the
  entire score engine — exactly the audit's predicted blind spot (a reclaim
  scores poorly on trend-confirmation at its onset, so the engine only sees
  it after it has run).
- **Breakout, as classically parameterized, earns nothing**: rare (0.39/day),
  25% WR, −0.43R. n=16 is too small to damn the concept, but it is far too
  weak to spend paper-book capacity on — and tuning parameters until it
  looks better is the overfit this repo keeps refusing. ORB already failed
  here once (audit §16 P3 note).
- Caveats: underlying R is theta-blind and spread-blind; the paper book
  (premium truth, net of charges) remains the deciding evidence. One 60-day
  window; one a-priori parameterization.

**Decision: v1 ships ONE detector — VWAP cross — shadow-only. Breakout is
deferred**, to be redefined (if at all) from eval-trace miss shapes and
re-sized offline before any wiring.

## v1 design (VWAP cross, shadow-only)

- `app/signals/setups.py`: pure detector on the intraday 3m closed-candle
  frame. Fire when ≥8 consecutive closes sat on one side of session VWAP and
  the current close crosses it on volume ≥1.2× the 20-bar median. Both
  directions always — **no regime lockout** (the lockout is the #1 measured
  blind-spot cause). 30-min per-direction dedupe.
- Wiring in `SignalService.evaluate_symbol`: when the engine offers no clean
  card, run the detector on the same df. A hit builds a card through the
  EXISTING machinery (strike.select liquidity guards, risk ladder, freshness
  gate) so the simulated trade is exactly what the system would offer —
  tagged `hollow_reason = "setup: vwap_cross …"`.
- Its own shadow store (`setup_shadow_store`, the level-watch/store lesson:
  no slot sharing with floor/late/refire) and its own paper capacity bucket
  via `shadow_class() == "setup"`. Ledger block `setup_shadow` in
  /paper/summary keyed **per setup name** — per-setup expectancy from day
  one, as the audit demands.
- Participation floors apply unchanged (volume is baked into the detector;
  the OI floor runs on the card). Confounded fires (would also be floor/late
  vetoed) book NOWHERE — same purity rule as every other ledger.
- **No pushes, no dashboard presence, no live cards.** Paper + a Lab line
  only. `SETUP_LIVE_ENABLED` does not even exist yet; it gets created the
  day the evidence earns it.
- Eval trace gains a `setup` field so misses/hits stay classifiable.

## Evidence gates (pre-registered, same as scalp's discipline)

- Verdict at **30+ paper fills**: positive net expectancy after charges →
  propose promotion (advisory cards behind a default-off flag); negative →
  retire the detector, keep the ledger as the record.
- Parameters are frozen as registered above. Any change resets the fill
  counter to zero — a tuned detector is a new detector.
- At 1.24 fires/day the 30-fill verdict needs roughly 5–6 weeks. That is the
  cost of honest evidence; nothing here shortcuts it.

## Sequencing

1. Build v1 (detector + wiring + tests + adversarial review) — one session.
2. Let it collect alongside the level-watch callouts, the refire/stop-basis
   ledgers and the eval trace (all live as of 03-Aug).
3. Revisit breakout only after the eval trace has enough sessions to say
   what the uncaught moves actually look like at onset — then size any new
   definition on this same replay harness BEFORE wiring it.
4. If threshold-flicker shows up in the setup stream, P2's WATCH→CONFIRM
   persistence layer applies to setups too.
