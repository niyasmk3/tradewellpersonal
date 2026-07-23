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
#   ./start.sh          start backend (caffeinate + uvicorn) and frontend
#   ./start.sh stop     stop whatever is on the two ports
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
