#!/usr/bin/env bash
# Daily 08:40 IST (launchd: com.tradewell.morning): Zerodha flushes access
# tokens ~07:30, but the backend trusts its on-disk cache and boots "logged
# in" with a dead token — the login screen then never appears (03-Aug lesson).
# Fix: before each session, drop the stale cache and bounce the stack so the
# dashboard greets the user with the login screen, ready for the one manual
# step of the day.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/recorder/data/morning_reset.log"
say() { printf '%s %s\n' "$(TZ=Asia/Kolkata date '+%d-%m %H:%M:%S')" "$*" >> "$LOG"; }

DOW=$(TZ=Asia/Kolkata date +%u)
[ "$DOW" -gt 5 ] && exit 0

say "morning reset: clearing stale token cache and bouncing stack"
launchctl unload "$HOME/Library/LaunchAgents/com.tradewell.stack.plist" 2>>"$LOG"
sleep 2
rm -f "$ROOT/backend/.kite_session.json"
launchctl load "$HOME/Library/LaunchAgents/com.tradewell.stack.plist" 2>>"$LOG"
say "stack rebooted fresh — login screen ready"
