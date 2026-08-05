#!/bin/bash
# Deploy-at-close: wait for 15:31 IST, then restart the backend in place and
# verify it came back. Used to load committed code without touching a live
# session. (Lived in the session scratchpad before 05-Aug; a temp cleanup ate
# it mid-countdown — ops tooling belongs in the repo.)
while true; do
  hm=$(TZ=Asia/Kolkata date +%H%M)
  [ "$hm" -ge 1531 ] && break
  sleep 60
done
echo "=== $(TZ=Asia/Kolkata date '+%H:%M:%S') firing restart ==="
curl -s -m 5 -X POST http://localhost:8000/system/restart; echo
sleep 15
for i in $(seq 1 20); do
  out=$(curl -s -m 2 http://localhost:8000/auth/status 2>/dev/null)
  [ -n "$out" ] && break
  sleep 3
done
echo "auth: $out"
echo "--- health ---"
curl -s -m 3 http://localhost:8000/health
echo
