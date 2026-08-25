# MCX Gold research module — experiment-only

A second evidence stream, built the house way: measure first, believe later,
risk capital never (and live trading is not even on the roadmap here).
Lives entirely in `recorder/mcx/` — its own DB (`data/mcx_history.db`), its
own docs, zero contact with the upstream app and zero mixing with the NIFTY
card dataset.

## Why MCX GOLD (decided 25-Aug-2026)

- **Legal.** Offshore forex/CFD platforms (XAUUSD brokers, OctaFX-style
  apps) are FEMA violations for Indian residents — RBI keeps an alert list.
  The lawful Indian routes are MCX commodity derivatives and NSE currency
  derivatives. Gold on MCX via Zerodha is fully legal and uses the same
  Kite login we already hold.
- **Beginner-suitable vs the alternatives.** Public guidance consistently
  rates gold the gentlest MCX commodity (clear levels, fewer news shocks);
  crude/natgas can move 2–7% on one OPEC/inventory headline; USDINR barely
  moves at all (RBI-managed float) so charges dominate.
- **Complementary clock.** First climatology (below): gold's action is in
  the EVENING — no attention conflict with NIFTY day trading.

## First climatology (GOLD_FUT, ~120 sessions of 3m bars, 250 day-candles)

- Movement by hour: 17:00–21:00 IST carries ~42% of all intraday travel
  (18:00–20:00 alone ~29%) — London PM / COMEX open / US data window.
  Midday 12:00–16:00 is quiet (~5%/hour). The 09:00 open hour bursts (8.7%)
  as MCX catches up to overnight international moves.
- Avg daily range 1.88%. Avg overnight gap 0.65%; 45% of days gap >0.5% —
  international gold trades while MCX sleeps, so POSITIONAL ideas carry
  real gap risk and must be studied as gap-exposed from day one.

## What transfers from the NIFTY lab (and what does not)

Transfers: Kite auth + chunked backfill, SQLite schema, the walk-forward
harness pattern, pre-registration discipline, 30-sample kill checkpoints,
the promotion-to-live bar in ../experiment/PROGRAM.md (applies verbatim).

Does NOT transfer: the card dataset. Gold has no signal engine issuing
cards — fills here start at ZERO and accumulate at market speed. No
shortcut exists. What gold has instead: candle-only strategies are honestly
backtestable point-in-time (no unrecordable chain/news inputs), so
walk-forward backtests are legitimate here in a way they never were for
NIFTY cards.

## Phases

- [x] **P0 (25-Aug):** backfill_mcx.py — GOLD/GOLDM day ~5y + 3m/15m
      current contract. 56,261 bars landed.
- [ ] **P1:** nightly refresh + climatology page section (session
      structure, day-types, gap stats, vol regimes) — automatic, no
      opinions, published to the research page.
- [x] **P2 (frozen 25-Aug, same day):** three rules pre-registered in
      hypotheses.py — H1 gap-fade, H2 burst-continuation, H3 US-window
      follow — parameters chosen from climatology magnitudes BEFORE any
      grading, then backtested once on all stored MCX 3m sessions (~110
      days, GOLDM 1 lot, ~Rs250 RT charges, idealised exits):
        H1-gapfade   35 trades  9W  net −Rs78,806  ← gaps RUN, fading them bled
        H2-burst     37 trades 17W  net −Rs10,578
        H3-uswindow  44 trades 23W  net +Rs25,267  ← only survivor so far
      Lessons recorded: (a) two of three pre-registered ideas would have
      lost money — this is exactly what the freeze-then-grade order is for;
      (b) the tempting "so fade H1 → gap CONTINUATION must win" is a NEW
      hypothesis (H4) that must be pre-registered separately if ever tried,
      never a sign-flip of a failed rule; (c) backtest is one ~110-session
      period with idealised exits — the FORWARD shadow ledger (started
      25-Aug, TRIAL cards on gold.html) is the only evidence that counts
      toward promotion: 30+ forward samples per rule, then verdicts.
- [ ] **P3:** only if something survives 100+ episodes with the PROGRAM.md
      live bar: discuss next steps. Not before.

## Rules inherited verbatim

Holdout periods are radioactive. Every attempt is logged, never deleted.
One idea per experiment. A 30-sample verdict is an early KILL checkpoint,
never proof. Nothing promotes automatically.
