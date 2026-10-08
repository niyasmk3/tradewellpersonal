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

# TZ-proof scheduling (08-Oct-2026): launchd calendar triggers follow the
# Mac's LOCAL clock — with the Mac left on Asia/Dubai this fired at 10:10
# IST for days, killing the live session mid-market. Now launchd runs this
# every 5 min with SCHEDULED=1 and the script decides IN IST whether it is
# 08:40–08:59 and not yet done today. Manual runs (no SCHEDULED) always go.
if [ "${SCHEDULED:-}" = "1" ]; then
  NOW=$(TZ=Asia/Kolkata date +%H%M); TODAY=$(TZ=Asia/Kolkata date +%F)
  MARK="$ROOT/recorder/data/.morning_done"
  [ "$(cat "$MARK" 2>/dev/null)" = "$TODAY" ] && exit 0
  { [ $((10#$NOW)) -ge 840 ] && [ $((10#$NOW)) -lt 900 ]; } || exit 0
  echo "$TODAY" > "$MARK"
fi

say "morning reset: clearing stale token cache and bouncing stack"
launchctl unload "$HOME/Library/LaunchAgents/com.tradewell.stack.plist" 2>>"$LOG"
sleep 2
rm -f "$ROOT/backend/.kite_session.json"
launchctl load "$HOME/Library/LaunchAgents/com.tradewell.stack.plist" 2>>"$LOG"
say "stack rebooted fresh — login screen ready"
