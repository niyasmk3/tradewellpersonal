# Gold / forex research module — implementation spec (no design opinions)

**From:** Niyas's out-of-tree research deployment · 27-Aug-2026
**What this is:** everything needed to implement the gold (MCX + XAUUSD)
shadow-research module in your own style. Logic, data, methodology, and the
lessons already paid for — nothing about UI, layout, or code organization.
Reference implementation exists in my recorder (paths at the end) but is
deliberately NOT a style suggestion.

---

## 1. Purpose and stance

A second evidence stream beside NIFTY: pre-registered, candle-only trading
rules on gold, graded shadow-first exactly like the condor — advisory
nothing, paper everything, promotion only through forward samples. Gold was
chosen over crude (2–7% news spikes) and USDINR (RBI-managed, no range) for
its session structure and beginner-tolerant behavior.

Two venues, one metal, by design:
- **MCX GOLD futures** — the legal Indian venue today (resident + commodity
  segment). NOTE: NRIs cannot trade MCX commodity derivatives at all.
- **XAUUSD international spot** — the venue an NRI can legally trade from
  abroad via regulated brokers (offshore forex is a FEMA violation for
  residents). Rules that survive on one venue can be re-validated on the
  other; our first climatology showed the two venues share the same clock.

## 2. Data sources (all free, all verified working)

**MCX candles via Kite historical API** (same auth as the main app):
- Instruments: `kite.instruments("MCX")`, filter name in (GOLD, GOLDM),
  instrument_type FUT, nearest expiry.
- Day candles: `continuous=1`, ~5 years available.
- Intraday (3m/15m): current contract only, ~120 days back (contract
  listing limit; `continuous` is rejected for intraday intervals).
- MCX session: Mon–Fri 09:00–23:30 IST (evening driven by London/US).

**XAUUSD 1-minute candles via Dukascopy's public datafeed** (no account):
```
https://datafeed.dukascopy.com/datafeed/XAUUSD/{yyyy}/{MM}/{dd}/BID_candles_min_1.bi5
```
Hard-won specifics:
- **The month in the URL is ZERO-indexed** (July = "06").
- One file per UTC day; weekends/holidays 404 (normal).
- Format: LZMA-compressed ("alone" format); after decompress, 24-byte
  big-endian records: `>5If` = seconds-from-day-start, open, close, low,
  high (integers, divide by 1000 for USD), volume float32. Note the field
  order: open, CLOSE, LOW, high.
- Their edge rejects non-browser TLS clients with 503 (python-requests
  fails even with browser headers). **curl with a browser User-Agent and
  `Referer: https://freeserv.dukascopy.com/` passes.** Use curl (or match
  its TLS fingerprint) as the transport.
- ~940 weekday files ≈ 3 years ≈ 1.3M bars ≈ a few minutes with 8-way
  concurrency. Make ingestion resume-safe (skip days already stored) and
  sanity-band the parsed prices (reject a whole day outside e.g.
  500–20,000 USD — protects against silent format drift).

**Suggested storage:** one candles table keyed (symbol, timeframe, bar_ts)
with OHLCV — same shape as any other candle store.

## 3. Climatology first (before any rule exists)

Compute and publish base rates before designing rules — they are the
priors that stop small-sample lies later. What 120 MCX sessions + 900
XAUUSD sessions showed:

- **Gold is an evening animal**: 17:00–21:00 IST carries ~42% of intraday
  travel; 18:00–20:00 (US open) is the peak. Confirmed independently on
  both venues — same busiest hours (19, 18, 20 IST) from two unrelated
  data sources. XAUUSD adds a 06:00 IST Asian-open bump MCX cannot see.
- Average daily range ~1.9%; average overnight gap 0.65%; **45% of days
  gap >0.5%** (international trading continues while MCX sleeps) — any
  overnight idea must be treated as gap-exposed from birth.
- Label-stability lesson (from the NIFTY tape study, applies generally):
  a session-shape label measured mid-day changes by afternoon on ~half of
  days — treat such labels as moment-context, never day-type.

## 4. The rule framework (the actual method)

**Pre-registration contract:** rules are written, parameterized, and FROZEN
before any grading. Editing a parameter after grading = the experiment is
dead; a new idea gets a new rule name. Choose thresholds from climatology
magnitudes, not from outcome peeking.

The three rules frozen 25-Aug (parameters included so results below are
reproducible):

| Rule | Qualify | Entry | Direction | Target | Stop | Exit by |
|---|---|---|---|---|---|---|
| H1 gap-fade | \|overnight gap\| ≥ 0.5% | first 3m close ≥ 09:15 | against gap | previous close | 0.35% | 17:00 |
| H2 burst-follow | \|09:00→09:30 move\| ≥ 0.15% | first close ≥ 09:30 | with move | 0.30% | 0.25% | 15:00 |
| H3 US-window follow | \|17:00→18:00 move\| ≥ 0.10% | first close ≥ 18:00 | with move | 0.35% | 0.30% | 21:00 |

Simulation semantics (identical for backtest and live — see §5):
- Entries at bar closes. Stop/target tested against subsequent bars'
  high/low; **if both are touched inside one bar, the stop wins**
  (conservative tie-break). Time exit at the deadline's last close.
- Sizing for P&L: GOLDM mini (100g) = ₹10 per point of the per-10g quote;
  ~₹250 round-trip charges assumed. Exits are idealised (filled at the
  level, no slippage model yet) — label this caveat visibly; the forward
  book's job includes measuring how honest it is.

## 5. The one non-negotiable architecture decision

**One code path grades everything.** The same `simulate_day(bars,
prev_close, live?)` function must produce: the historical backtest, the
nightly ledger rebuild, AND the live open-card state. This makes
backtest-vs-live drift structurally impossible and makes the whole ledger
a deterministic cache of raw candles — rebuild it nightly from the candle
store; never persist it as independent truth.

**Backtest/forward split:** every simulated trade is stamped by day and
partitioned against the freeze date. Days before freeze = backtest
(hindsight-risked homework); days after = forward (blind exam). **Only
forward samples count toward any promotion.** Display both, separately,
always.

**Verdict gates** (same as the house rules): 30+ forward samples per rule
before any keep/kill verdict; the live-money bar is far higher (100+
forward episodes, positive net expectancy after all charges,
clustered-by-day CI above zero, profit factor >1.2, no single day >25% of
profit, 2+ regimes).

## 6. Live evaluation loop

- Poll today's 3m candles once per ~25–60s during MCX hours (one
  historical-API call; rate-trivial). Recompute `simulate_day(live=True)`
  each tick — stateless recomputation makes the loop restart-proof, no
  open-position persistence needed.
- Card states per rule: no-setup / open (with entry, live P&L, target px,
  stop px, window) / closed (exit reason + net). Alert on state
  transitions only, suppress on first pass after restart (no replay spam),
  and label every alert as paper/TRIAL.
- Fail-soft everywhere: stale token → retry after next login; any error
  must never crash the loop or block the main app's pipeline.

## 7. Results so far (for calibration, not authority)

Backtest (~110 MCX sessions, pre-freeze): H1 **−₹78,806** (35 trades — gaps
RUN; fading them bled), H2 −₹10,578 (37), H3 +₹25,267 (44, the only
survivor). Cross-check: NIFTY 3-year climatology independently shows
gaps ≥0.5% fill same-day only 19% of the time — gap-fade is likely
structurally poor on Indian index tape too.

Forward (first 3 days, n=4 — a STORY, not evidence): H1 +14,250 (1),
H2 +9,190 (2), H3 −5,111 (1). Note the rankings inverted vs backtest —
which is precisely why the 30-sample gate exists. Do not let early forward
results promote or kill anything.

## 8. Compliance notes worth embedding

- NRIs: no MCX commodity derivatives, period. XAUUSD via regulated brokers
  becomes legal only with genuine non-resident status.
- Offshore forex/CFD platforms are FEMA violations for Indian residents.
- Advisory-only posture throughout; distributing actionable signals to
  others can trip SEBI RA/IA rules (same note as the existing webhook
  warning).

## 9. Reference implementation (logic reference only, not style)

In my recorder (out-of-tree, own repo): `mcx/hypotheses.py` (rules +
simulate_day — the one code path), `mcx/backfill_mcx.py` and
`mcx/backfill_xauusd.py` (ingestion incl. the bi5 parser and curl
transport), `mcx/nightly_gold.py` (ledger rebuild + climatology),
`mcx/gold_live.py` (live loop + alerts). All MIT-spirit: take anything,
restyle everything.
