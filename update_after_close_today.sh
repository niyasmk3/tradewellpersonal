#!/usr/bin/env bash
# One-shot for 04-Aug: pull the owner's 22-commit update (Patterns Lab,
# callouts, P1 audit batch) AFTER close and restart the stack on the new
# code. No dependency or env changes upstream, so pull + bounce is the whole
# deploy. Logs to recorder/data/after_close.log.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/recorder/data/after_close.log"
say() { printf '%s %s\n' "$(TZ=Asia/Kolkata date '+%d-%m %H:%M:%S')" "$*" >> "$LOG"; }

say "waiting for 15:35 IST to deploy upstream update"
while [ "$(TZ=Asia/Kolkata date +%H%M)" -lt 1535 ]; do sleep 120; done

cd "$ROOT"
say "pulling origin/main"
if git pull --ff-only origin main >> "$LOG" 2>&1; then
  say "pull ok at $(git rev-parse --short HEAD)"
else
  say "PULL FAILED — leaving stack on current code"; exit 1
fi

say "restarting stack on new code"
launchctl unload "$HOME/Library/LaunchAgents/com.tradewell.stack.plist" 2>>"$LOG"
sleep 2
launchctl load "$HOME/Library/LaunchAgents/com.tradewell.stack.plist" 2>>"$LOG"
say "update deployed"
