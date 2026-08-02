# Sidecar recorder (local only — NOT part of the upstream repo)

Records what the backend throws away at close: option-chain OI snapshots,
tape pulse, indicators, market snapshots and candles — into
`data/tradewell_history.db` (SQLite, zlib-compressed JSON payloads).
This is the training dataset for future ML work.

This folder is listed in `.git/info/exclude` (local gitignore), so it is
invisible to `git status` / `git pull` and can never conflict with upstream.

## Run

Runs automatically via launchd (`com.tradewell.recorder`), Mon–Fri
09:05–15:40 IST whenever the backend is up. Manual controls:

```bash
launchctl unload ~/Library/LaunchAgents/com.tradewell.recorder.plist   # stop
launchctl load   ~/Library/LaunchAgents/com.tradewell.recorder.plist   # start
tail -f recorder/data/recorder.log                                     # watch
```

Test one cycle by hand: `backend/.venv/bin/python recorder/record.py --once`

## Read the data (pandas)

```python
import sqlite3, zlib, json, pandas as pd
db = sqlite3.connect("recorder/data/tradewell_history.db")
candles = pd.read_sql("SELECT * FROM candles WHERE symbol='NIFTY' AND tf='3m'", db)
chains = [
    (ts, json.loads(zlib.decompress(blob)))
    for ts, blob in db.execute(
        "SELECT ts_ist, payload FROM snapshots WHERE endpoint='chain' AND symbol='NIFTY'"
    )
]
```

Endpoints recorded: `chain` (per symbol, 60s), `pulse` (60s), `indicators`
(60s), `market_snapshot` (60s), `mood` (30 min), plus deduped 3m/15m candles
(5 min). Roughly 5–10 MB/day compressed. Additionally `card_birth`: the
moment a new signal card appears, one combined chain+pulse+indicators
snapshot is captured immediately and tagged with the card id (deduped across
restarts via the `card_births` table) — the precise at-issue market state
for training joins.

## Companion tools

- `backfill.py` — one-time pull of PAST charts (continuous futures 3m/15m/day,
  spot indices + INDIA VIX daily) into the same DB. Needs Kite keys plus one
  day's dashboard login. Idempotent.
- `analyze.py` — one-screen summary of every evidence file.
- `TRAINING.md` — the full guide: data → features → walk-forward training →
  shadow deployment, plus the improvement roadmap.
