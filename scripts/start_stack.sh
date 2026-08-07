#!/bin/bash
# Cold-start the whole stack after a reboot: backend engine (:8000) and the
# dashboard (:3000). Idempotent — a port that is already listening is left
# alone, so running this twice (or while half the stack survives) is safe.
#
# Born 07-Aug: the Mac restarted overnight, both processes died, and the
# engine was still down at the 09:15 open. The in-app supervisor can heal a
# sick ticker but nothing revives a process the OS took down — that job needs
# a script OUTSIDE the processes it starts. Run from any directory:
#
#   ./scripts/start_stack.sh
#
# What it cannot do: the daily Kite login. The access token expires every
# morning, so after a start the script prints the auth state — when it says
# NOT AUTHENTICATED, open http://localhost:3000 and complete the Kite login;
# the feed and every loop start the moment auth lands.

set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

up() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# --- backend :8000 -----------------------------------------------------------
# cwd must be backend/: every state file (.signals.json, the shadow ledgers,
# .daily_ops.json) is opened relative to the process cwd.
if up 8000; then
  echo "backend  :8000 already running — left alone"
else
  echo "backend  :8000 starting..."
  (cd backend && nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 \
      >> ../logs/backend.log 2>&1 &)
  for _ in $(seq 1 15); do
    curl -s -m 2 http://localhost:8000/health >/dev/null 2>&1 && break
    sleep 2
  done
fi

# --- frontend :3000 ----------------------------------------------------------
if up 3000; then
  echo "frontend :3000 already running — left alone"
else
  echo "frontend :3000 starting..."
  (cd frontend && nohup npm run dev >> ../logs/frontend.log 2>&1 &)
  for _ in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://localhost:3000 2>/dev/null)
    [ "$code" = "200" ] && break
    sleep 2
  done
fi

# --- report ------------------------------------------------------------------
echo "--- health ---"
health=$(curl -s -m 3 http://localhost:8000/health 2>/dev/null)
echo "${health:-backend did not come up — see logs/backend.log}"
code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 http://localhost:3000 2>/dev/null)
echo "frontend :3000 -> HTTP ${code}"
case "$health" in
  *'"authenticated":false'*)
    echo ""
    echo ">>> NOT AUTHENTICATED — open http://localhost:3000 and do the Kite login."
    ;;
esac
