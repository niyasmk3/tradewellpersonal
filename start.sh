#!/usr/bin/env bash
# Tradewell — one command for the whole stack, in YOUR terminal.
#
# WHY THIS EXISTS: the stack died three separate times on 23-Jul — twice the
# frontend (it was running under an assistant session that slept with the
# laptop) and once the backend's tick feed (the websocket reactor cannot be
# rebuilt in-process after a sleep). The fixes are the same every time:
# processes owned by your own terminal, and a backend wrapped in caffeinate so
# the Mac cannot sleep mid-session. This script is that, plus clean shutdown.
#
#   ./start.sh                   start backend (caffeinate + uvicorn) and frontend
#   ./start.sh stop              stop whatever is on the two ports
#   ./start.sh --live-override   start even during market hours (see guard below)
#   ./start.sh install-launchd   install the login auto-start (see deploy/)
#
# Ctrl+C in the running script stops BOTH servers — no orphans left holding
# the ports, which is what made "localhost not working" a recurring mystery.
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PORT=8000
FRONTEND_PORT=3000
LOGS="$ROOT/logs"

say() { printf '\033[1;36m[tradewell]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[tradewell]\033[0m %s\n' "$*" >&2; exit 1; }

port_pids() { lsof -ti tcp:"$1" 2>/dev/null; }

stop_port() {
  local pids
  pids=$(port_pids "$1")
  if [ -n "$pids" ]; then
    say "stopping existing process on :$1 (pid $pids)"
    kill $pids 2>/dev/null
    sleep 1
  fi
}

if [ "${1:-}" = "stop" ]; then
  stop_port "$BACKEND_PORT"
  stop_port "$FRONTEND_PORT"
  say "stack stopped"
  exit 0
fi

if [ "${1:-}" = "install-launchd" ]; then
  # Ship the login auto-start. Installs the plist with this checkout's path
  # baked in, but does NOT load it — loading is a one-line decision the user
  # makes once, with the file in front of them, not a side effect of a script.
  PLIST_SRC="$ROOT/deploy/tradewell.plist"
  PLIST_DST="$HOME/Library/LaunchAgents/com.tradewell.stack.plist"
  [ -f "$PLIST_SRC" ] || die "deploy/tradewell.plist missing from this checkout"
  mkdir -p "$HOME/Library/LaunchAgents" "$LOGS"
  sed "s|__TRADEWELL_ROOT__|$ROOT|g" "$PLIST_SRC" > "$PLIST_DST"
  say "installed $PLIST_DST (not loaded)"
  say "to enable auto-start at login:   launchctl load $PLIST_DST"
  say "to disable it again:             launchctl unload $PLIST_DST"
  say "NOTE: 'launchctl load' starts the stack IMMEDIATELY (RunAtLoad), not"
  say "just at the next login — and it restarts whatever is on the ports."
  say "Run it outside market hours, like any other deploy."
  exit 0
fi

# MARKET-HOURS DEPLOY GUARD. A restart kills the tick feed, resets warm-up and
# re-arms every monitor mid-session — the worst possible moment to discover a
# code change misbehaves. Deploys land after 15:30; a mid-session start must
# say --live-override out loud (crash recovery is what the flag is FOR — the
# launchd job uses it, because at login nothing is running and bringing the
# stack back IS the recovery).
if [ "${1:-}" != "--live-override" ]; then
  IST_DOW=$(TZ=Asia/Kolkata date +%u)   # 1=Mon … 7=Sun
  IST_HM=$(TZ=Asia/Kolkata date +%H%M)
  if [ "$IST_DOW" -le 5 ] && [ "$IST_HM" -ge 0915 ] && [ "$IST_HM" -le 1530 ]; then
    # "inside market hours", not "market is open" — this guard cannot see NSE
    # holidays, and claiming an open market on Independence Day would be false.
    die "inside market hours (Mon-Fri 09:15-15:30 IST; now $IST_HM, holidays not tracked)
            — restarting would drop a live feed. Deploy after 15:30, or run:
            ./start.sh --live-override   (crash recovery / holiday)"
  fi
fi

[ -x "$ROOT/backend/.venv/bin/python" ] || die "backend/.venv missing — create it and pip install -r requirements first"
[ -f "$ROOT/backend/.env" ] || die "backend/.env missing — copy backend/.env.example and fill in your Kite keys"
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  say "frontend/node_modules missing — running npm install (one-time)"
  (cd "$ROOT/frontend" && npm install) || die "npm install failed"
fi

mkdir -p "$LOGS"

# A previous half-dead instance holding a port is the usual cause of
# "localhost not working" — clear both ports before starting.
stop_port "$BACKEND_PORT"
stop_port "$FRONTEND_PORT"

# caffeinate -s: the Mac may not sleep while the backend lives. A sleeping
# laptop is a dead tick feed, and a dead feed during market hours is how the
# 12:30 doubling on 23-Jul went unsignalled.
say "starting backend on :$BACKEND_PORT (caffeinate keeps the Mac awake)"
(cd "$ROOT/backend" && exec caffeinate -si .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$BACKEND_PORT") >>"$LOGS/backend.log" 2>&1 &
BACKEND_PID=$!

say "starting frontend on :$FRONTEND_PORT"
(cd "$ROOT/frontend" && exec npm run dev) >>"$LOGS/frontend.log" 2>&1 &
FRONTEND_PID=$!

cleanup() {
  say "shutting down…"
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null
  wait 2>/dev/null
  say "stack stopped"
  exit 0
}
trap cleanup INT TERM

# Wait for both to answer before declaring victory, so "started" means
# "usable", not "processes exist".
for i in $(seq 1 60); do
  ok_b=$(curl -s -m 1 -o /dev/null -w '%{http_code}' "http://localhost:$BACKEND_PORT/auth/status" || true)
  ok_f=$(curl -s -m 1 -o /dev/null -w '%{http_code}' "http://localhost:$FRONTEND_PORT" || true)
  [ "$ok_b" = "200" ] && [ "$ok_f" = "200" ] && break
  sleep 1
done

if [ "${ok_b:-}" = "200" ] && [ "${ok_f:-}" = "200" ]; then
  say "dashboard:  http://localhost:$FRONTEND_PORT"
  say "backend:    http://localhost:$BACKEND_PORT  (logs: logs/backend.log)"
  say "remember the daily Kite login if the feed shows stale"
  say "Ctrl+C here stops both servers"
else
  say "servers started but not answering yet — check logs/backend.log and logs/frontend.log"
fi

wait
