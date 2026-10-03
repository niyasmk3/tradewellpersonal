# Gold module — field notes from an independent deployment (03-Oct-2026)

**From:** Niyas's out-of-tree recorder (the reference implementation your
module cites). Pull-only deployment; nothing pushed, no tracked files edited.

## 1. Dukascopy sync: the http fallback is now stale, and bursts get 503'd

On this machine (macOS curl, same UA/Referer as yours) the first
`/gold/sync-xau` stored 63 of 782 days: https answered **503** under the
8-worker burst, the http fallback answered **301** (Dukascopy now redirects
plain http to https — the "plain HTTP serves 200" assumption no longer holds),
and by the end curl was failing with exit 6 (DNS) — the edge was throttling
the IP. Our recorder fetches the same files successfully every night with a
gentle 7-day window; the difference appears to be burst size, not headers.
Suggestions: drop the http scheme (it only yields 301 now), add per-request
pacing/backoff and a smaller concurrency for the initial backfill, and keep
the resume-safe day-status logic (it worked perfectly — re-runs only touched
unrecorded days).

On this deployment I seeded `.gold_xau.db` from our archive (1,344,960 bars,
934 days, meta key `seeded_from`) so `/analyze` could run. Both stores now
agree bar-for-bar on overlapping days.

## 2. Cross-check result: the two implementations are the same rules

Day-by-day join of forward trades on identical GOLDM 3m bars:
**43/43 trades qualified with identical direction; fills within a few
points on ~35 of 43.** Backtest hit-rate ordering agrees (H1 ≈ 26% worst,
H3 best, H2 middle). The port is faithful.

## 3. The one real finding: H3 is decided by a one-bar entry convention

Your entry = first bar whose **close time** ≥ 18:00 (the 17:57 bar). Ours =
first bar **opening** at 18:00 (closing 18:03). On US-release evenings that
bar is the release itself:

| Evening | Your H3 | Ours (same bars) | 18:00 bar |
|---|---|---|---|
| 04-Sep (NFP) | LONG 155,368 → stop −4,911 | LONG 152,239 → target +5,078 | 155,391 → low 152,010 → 152,239 |
| 10-Sep (CPI) | SHORT 152,710 → stop | SHORT 152,324 → target | 152,761 → 152,324 |

Forward H3 therefore reads −₹27,415 in your ledger, +₹6,497 in ours on
GOLDM, +₹10,979 in ours on GOLD — one rule, three honest readings, opposite
signs. The spec I sent said "first 3m close ≥ 18:00" without pinning the
bar-stamp convention; that's on me. But the deeper point is structural: H3's
entry sits on the loudest minute of gold's day, so no convention makes it
robust — a human or an algo entering "at 18:00" on NFP gets neither print.

I'd suggest (a) stating the bar convention explicitly in the rule table so
both labs read the same bar, and (b) treating H3's forward as unproven
regardless of sign. We are not editing H3 (freeze rule); a post-release-
candle variant or an event-evening skip would be a new H4 on its own clock.

## 4. Minor

- `/gold/live` and `/gold/results` return 404 until the first analyze; the
  tab shows a blank rather than "run sync first". A hint would save a
  support question.
- `GOLD_LIVE_ENABLED=true` + the 45s poll ran all week without incident
  beside the recorder's own feeder.
