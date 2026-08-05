#!/usr/bin/env bash
# Self-healing watchdog (launchd: com.tradewell.uptime, every 5 min).
# The Next dev server has now been silently killed twice (memory pressure —
# no crash log, just SIGKILL). The user declined phone alerts, so instead of
# paging we HEAL: if the stack job is loaded but either port is dead across
# two probes 5s apart, kick the whole job. A deliberate `launchctl unload`
# removes the job, so this guard sees it absent and does nothing — it never
# fights an intentional stop.
set -u
LOG="$HOME/Developer/Tradewell/recorder/data/uptime_check.log"
say() { printf '%s %s\n' "$(TZ=Asia/Kolkata date '+%d-%m %H:%M:%S')" "$*" >> "$LOG"; }

launchctl list | grep -q com.tradewell.stack || exit 0   # deliberately stopped

alive() {
  b=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8777/auth/status)
  f=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:3777/)
  [ "$b" = "200" ] && [ "$f" = "200" ]
}

alive && exit 0
sleep 5
alive && exit 0

say "stack unhealthy (backend=$b frontend=$f) — kicking com.tradewell.stack"
launchctl kickstart -k "gui/$(id -u)/com.tradewell.stack" 2>>"$LOG" \
  && say "kickstart issued" || say "kickstart FAILED"
