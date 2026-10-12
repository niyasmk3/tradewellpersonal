# Field report: the paper book closes positions on the first quote after an outage, unmarked

**From:** Niyas (independent deployment, pull-only — no changes made to tracked files)
**Date:** 13-Oct-2026
**Severity:** evidence-integrity (paper book records catch-up fills as rule exits; no live-order risk — app is advisory)

## TL;DR

When the backend is down for a while and comes back mid-session, the first
paper-monitor cycle finds every open position's stop / target / invalidation
already crossed and closes it **at that first tick**, stamped with the rule's
normal `auto_close_reason` and nothing to say a feed gap preceded it. On my
book five positional exits sit on the exact minute the backend returned,
including its two largest "wins" (+Rs 13,795 and +Rs 12,856), which are the
whole outage move booked as if the target had filled at its level. Suggested
fix: mark the first exit after a feed gap longer than N minutes with an
`exit_reason` such as **"catch-up after feed gap"** (keep `auto_close_reason`
for the rule that fired), stamp the gap length on the row, and exclude such
rows from book statistics, the exit A/B and the R&D twins.

## Observed evidence (my paper book, clean non-hollow fills)

| Trade | Contract | Mode | Entered (IST) | Exited (IST) | Reason | P&L | First quote of the day (condor 5-min store) | First recorder chain snapshot |
|---|---|---|---|---|---|---|---|---|
| T-7bd24304c2 | NIFTY 24100 CE (29-Sep) | positional | 28-Aug 15:20:11 | **31-Aug 11:58:44** | invalidation | −3,534.70 | 31-Aug 11:58:44 | 11:59:26 |
| T-0a2fff2989 | NIFTY 22650 PE (27-Oct) | positional | 30-Sep 14:37:21 | **01-Oct 13:49:22** | target1 | +12,855.70 | 01-Oct 13:49:22 | 13:50:26 |
| T-50f8df1e98 | NIFTY 22700 PE (27-Oct) | positional | 30-Sep 15:00:17 | **01-Oct 13:49:22** | target1 | +13,794.95 | 01-Oct 13:49:22 | 13:50:26 |
| T-7062c51313 | NIFTY 22500 PE (27-Oct) | positional | 08-Oct 09:44:27 | **09-Oct 10:39:10** | stop | −1,805.70 | 09-Oct 10:39:10 | 10:39:23 |
| T-c5d9be1e26 | NIFTY 22400 PE (27-Oct) | positional | 08-Oct 11:30:05 | **09-Oct 10:39:10** | invalidation | −5,717.40 | 09-Oct 10:39:10 | 10:39:23 |

On each of the three days neither store holds a single snapshot before the
exit minute: the outage ran from before the 09:15 open (2 h 44 min, 4 h 34
min and 1 h 24 min of session respectively) and the exit is stamped on the
very second the condor module took its first snapshot — i.e. the first
cycle after restart. The recorder, which polls the backend once a minute,
saw its first chain 13-66 s later. All five rows carry
`exit_price_source: "simulated"` and an ordinary `auto_close_reason`; nothing
on the row says the quote that priced them was the first one in hours.

The two 01-Oct target exits deserve a second look: NIFTY had fallen hard
during the gap, the puts were deep in profit when the backend returned, and
the monitor booked the entire move. A real target order would have filled
near the target level hours earlier, for a fraction of that. The stop and
invalidation cases are the mirror image — priced wherever the premium
happened to be, not at the level the rules name.

## Mechanism (as read in the code, current main)

- `backend/app/paper/service.py` — `run_once()` (~line 414) is the whole
  exit path. While the process is down nothing runs; the first cycle back
  calls `monitor.evaluate()` with the first tick, which is **fresh** by the
  feed watchdog's definition (`last_tick_age` is seconds old), so the stall
  disarm does not apply and every standard rule is live.
- `backend/app/trades/monitor.py` — `evaluate()` (~line 188) compares that
  first premium with stop / targets and the spot with the invalidation level;
  whatever was crossed during the gap fires now. The exit decision (~line 49)
  refuses a stale or absent premium, which is the right guard for the
  09:00 pre-open case I reported on 21-Aug, but the first post-restart tick
  passes it by construction.
- Nothing records the gap. The row gets `auto_close_reason` ("stop",
  "target1", "invalidation") and a simulated fill at that tick; the exit
  looks identical to a rule exit taken in a continuous session.

## Suggested fix direction (your call on specifics)

1. On every exit, compute the age of the contract's **previous** quote (the
   last tick the process saw for that token before this one, or "none since
   startup"). If it exceeds a threshold — I use 10 minutes — stamp
   `exit_reason = "catch-up after feed gap"` and `feed_gap_s` on the row,
   keeping `auto_close_reason` for the rule that fired.
2. Treat such rows as unknown-outcome in the book stats, the exit A/B
   ledgers and the R&D policy twins, the same way an off-session exit should
   be. Marking is honest; inventing a fill at the stop or target level is
   not, because a real order might or might not have filled there.
3. Optional: on startup, if open positions exist and the last persisted tick
   is older than the threshold, log one line per position ("catch-up
   evaluation after N min gap") so the operator sees it happen.

## Check your own book

```python
import json, sqlite3
from datetime import datetime, timedelta, timezone
IST = timezone(timedelta(hours=5, minutes=30))
db = sqlite3.connect("file:backend/.condor_chain.db?mode=ro", uri=True)
for t in json.load(open("backend/.paper_trades.json")):
    if not t.get("exited_at") or str(t.get("notes") or "").startswith("hollow"):
        continue
    x = int(t["exited_at"])
    day0 = int(datetime.fromtimestamp(x, IST).replace(hour=0, minute=0, second=0).timestamp())
    prev = db.execute("SELECT MAX(ts) FROM chain_snap WHERE token=? AND ltp>0 AND ts>=? AND ts<?",
                      (t["token"], day0, x - 120)).fetchone()[0]
    gap = (x - (prev or day0 + 9 * 3600 + 15 * 60)) / 60
    if gap > 10:
        print(t["id"][:12], t["contract"], datetime.fromtimestamp(x, IST), t.get("auto_close_reason"),
              t.get("realized_pnl"), f"{gap:.0f} min since the contract's previous quote")
```

(The condor store holds the monthly expiries at 5-minute cadence from
20-Aug; for weekly contracts use your own tick log or the recorder's chain
snapshots. The script treats the 09:15 open as the day's first possible
quote, so an overnight hold exiting at 09:16 is not reported.)

## What I did on my side

Nothing in the app was changed. In my recorder tooling the five rows are
quarantined from every statistic and training label (consumer-side
exclusion list plus a mechanical flag, `exit_after_feed_gap`, that re-checks
the whole book nightly: exit within 2 minutes of the first quote after a
gap longer than 10 minutes in that contract's own snapshots, the 09:15-09:20
opening window excepted). The exit twins already treated these fills as not
gradeable through their own gap rule.

## Provenance

Found 11-Oct while building the exit twins' 5-minute path on the condor
store (the holding windows of these five fills straddle the three outages);
each case then verified by hand against `.paper_trades.json`,
`.condor_chain.db` (table `chain_snap`) and the recorder's chain snapshots.
No tracked files were modified; nothing was pushed.
