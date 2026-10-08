#!/usr/bin/env bash
# Local port-remapped launcher (NOT part of the upstream repo — see
# .git/info/exclude). Runs the stack on exclusive ports so other dev projects
# on the usual :3000/:8000 never collide:
#
#   backend  :8777   frontend  :3777
#
# HOW: upstream's start.sh has the ports hardcoded, and editing a tracked file
# would eventually merge-conflict with the owner's changes. So this script
# sed-patches a COPY of start.sh at every launch and runs that. Upstream
# improvements to start.sh flow through automatically; the tracked file is
# never touched. The copy is generated into the repo root so start.sh's own
# ROOT="$(dirname "$0")" resolution still points at this checkout.
#
# Companion config (all untracked): backend/.env FRONTEND_ORIGIN, and
# frontend/.env.local NEXT_PUBLIC_API_BASE / NEXT_PUBLIC_WS_URL.
# Kite app redirect URL must be http://localhost:3777.
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/start.sh"
GEN="$ROOT/.start-ports.gen.sh"
B_PORT=8777
F_PORT=3777

[ -f "$SRC" ] || { echo "start.sh missing" >&2; exit 1; }

sed \
  -e "s/^BACKEND_PORT=8000$/BACKEND_PORT=$B_PORT/" \
  -e "s/^FRONTEND_PORT=3000$/FRONTEND_PORT=$F_PORT/" \
  -e 's/exec npm run dev/exec env PORT="$FRONTEND_PORT" npm run dev/' \
  "$SRC" > "$GEN"

# Fail loudly if upstream refactored start.sh and a pattern stopped matching —
# silently launching on the old ports would be worse than not launching.
if ! grep -q "BACKEND_PORT=$B_PORT" "$GEN" || \
   ! grep -q "FRONTEND_PORT=$F_PORT" "$GEN" || \
   ! grep -q 'env PORT="$FRONTEND_PORT" npm run dev' "$GEN"; then
  echo "local-start: start.sh changed upstream — port patch no longer applies." >&2
  echo "Re-check the sed patterns in local-start.sh against start.sh." >&2
  exit 1
fi

chmod +x "$GEN"
exec "$GEN" "$@"
