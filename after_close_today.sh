#!/usr/bin/env bash
# One-shot for today (03-Aug): wait for market close, then
#   1. load the stack launchd job (auto-start permanence; restarts the stack,
#      which is fine after 15:30 and exactly what the deploy rules want)
#   2. run the one-time historical backfill with today's still-valid session
# Logs to recorder/data/after_close.log. Safe to kill if plans change.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/recorder/data/after_close.log"
say() { printf '%s %s\n' "$(TZ=Asia/Kolkata date +%H:%M:%S)" "$*" >> "$LOG"; }

say "waiting for 15:35 IST"
while [ "$(TZ=Asia/Kolkata date +%H%M)" -lt 1535 ]; do sleep 120; done

say "loading stack launchd job (restarts stack post-close, then auto-starts at every login)"
launchctl load "$HOME/Library/LaunchAgents/com.tradewell.stack.plist" >> "$LOG" 2>&1 \
  && say "stack job loaded" || say "stack job load FAILED (may already be loaded)"

say "starting historical backfill (futures 3m/15m/day + indices + VIX)"
"$ROOT/backend/.venv/bin/python" "$ROOT/recorder/backfill.py" --days 365 >> "$LOG" 2>&1 \
  && say "backfill done" || say "backfill FAILED — see above"

say "after-close tasks complete"
