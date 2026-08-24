# Field report: paper monitor exits positions off-session on frozen quotes

**From:** Niyas (independent deployment, pull-only — no changes made to tracked files)
**Date:** 21-Aug-2026
**Severity:** evidence-integrity (paper book records fictional P&L; no live-order risk — app is advisory)

## TL;DR

The paper monitor keeps evaluating exits outside market hours. On my
deployment it has now twice "invalidated" an overnight positional at
**09:00 IST — before options trade at 09:15** — pricing the exit on the
previous session's frozen option premium. Both P&Ls are fiction. Suggested
fix: a market-session gate plus a per-instrument quote-age check on every
paper/journal auto-exit, so "next open" means the first fresh option print
after 09:15, not a date change.

## Observed evidence (my paper book, clean non-hollow fills)

| Contract | Mode | Entered (IST) | Exited (IST) | Reason | P&L |
|---|---|---|---|---|---|
| NIFTY 24350 CE | positional | 14-Aug 14:00:03 | **17-Aug 09:00:09** | invalidation | −664.95 |
| NIFTY 24250 CE | positional | 20-Aug 13:00:03 | **21-Aug 09:00:03** | invalidation | −1,800.50 |

Both are overnight positional holds; both exits are stamped ~09:00:0x —
15 minutes before the options market opens. The exit premium each time was
the prior session's last print (frozen since 15:30 the previous trading
day), while the invalidation was triggered by an early-morning underlying
value. The pattern will recur for any positional held overnight whose
invalidation level is breached by the pre-open underlying print.

## Mechanism (as read in the code, current main)

- `backend/app/paper/service.py` — `run_once()` (~line 414) runs on the
  service loop with no market-session gate on the exit path. The stall exit
  is correctly disarmed on stale ticks, but stop/target/invalidation
  evaluation still proceeds.
- `backend/app/trades/monitor.py` — `_invalidated()` (~line 178) fires on
  any single underlying value crossing the level. At ~09:00 the underlying
  snapshot updates (pre-open/indicative) while the option LTP cannot — so
  the exit both *triggers* off-session and *prices* off a frozen premium.
- There is a precedent for the right guard at issuance:
  `SIGNAL_MAX_PREMIUM_AGE_S` refuses to issue cards on stale premiums.
  Exits currently have no equivalent.

## Suggested fix direction (your call on specifics)

1. Gate paper and journal auto-exits to the trading session
   (Mon–Fri 09:15–15:30 IST, plus your square-off windows).
2. Require a fresh option quote (exchange timestamp within N seconds) to
   *price* any exit; hold the exit decision until the first fresh print
   otherwise — that makes "next open" honest by construction.
3. Optional: stamp `exit_quote_age_s` on the trade row so any future
   stale-priced exit is visible in the data rather than silent.

## Check your own book

```python
import json
from datetime import datetime, timedelta, timezone
IST = timezone(timedelta(hours=5, minutes=30))
for t in json.load(open("backend/.paper_trades.json")):
    if not t.get("exited_at") or str(t.get("notes") or "").startswith("hollow"):
        continue
    d = datetime.fromtimestamp(t["exited_at"], IST)
    if d.weekday() >= 5 or not (9*60+15 <= d.hour*60+d.minute <= 15*60+45):
        print(t.get("contract"), t.get("mode"), d, t.get("auto_close_reason"),
              t.get("realized_pnl"))
```

## Related observation (smaller, same neighbourhood)

`risk.py` derives invalidation from candle swing levels (and card copy reads
as candle semantics), but `_invalidated()` latches on a single tick/print.
The 09:00 incidents are the extreme case of that gap: one pre-open print
sufficed. Worth an explicit contract decision — tick-trigger vs
candle-close-confirm (+ a separate hard disaster stop) — applied identically
in live advice, paper, and any future replay.

## Provenance

Surfaced by an external automated review on 21-Aug; both incidents then
verified by hand against `.paper_trades.json` and the cited code paths. On
my side I have only quarantined the two rows from my own analytics
(consumer-side exclusion list + a mechanical off-session-exit filter in my
reporting tools). No tracked files were modified; nothing was pushed.
