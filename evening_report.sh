#!/usr/bin/env bash
# Nightly evidence report — runs after market close (launchd: Mon-Fri 16:00
# IST). Rebuilds the training dataset and writes the day's one-screen summary
# to recorder/data/reports/DD-MM-YYYY.txt. Read it with your evening tea;
# it is the project's scoreboard filling itself in.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
OUT_DIR="$ROOT/recorder/data/reports"
STAMP="$(TZ=Asia/Kolkata date +%d-%m-%Y)"
DOW="$(TZ=Asia/Kolkata date +%u)"   # 1=Mon … 7=Sun

[ "$DOW" -gt 5 ] && exit 0          # weekends: nothing traded, nothing to say

# TZ-proof scheduling (08-Oct-2026, see morning_reset.sh): launchd runs this
# every 5 min with SCHEDULED=1; the script decides IN IST whether it is
# 16:00–16:14 and not yet done today. Manual runs (no SCHEDULED) always go.
if [ "${SCHEDULED:-}" = "1" ]; then
  NOW=$(TZ=Asia/Kolkata date +%H%M); TODAY=$(TZ=Asia/Kolkata date +%F)
  MARK="$ROOT/recorder/data/.report_done"
  [ "$(cat "$MARK" 2>/dev/null)" = "$TODAY" ] && exit 0
  { [ $((10#$NOW)) -ge 1600 ] && [ $((10#$NOW)) -lt 1615 ]; } || exit 0
  echo "$TODAY" > "$MARK"
fi
mkdir -p "$OUT_DIR"

{
  echo "Tradewell evening report — $STAMP"
  echo "======================================================"
  # The launchd calendar jobs (morning 08:40, report 16:00) fire on the Mac's
  # LOCAL clock; everything else here computes IST itself. A Mac left on a
  # foreign timezone silently shifts those two jobs — found 08-Oct-2026 with
  # the Mac on Asia/Dubai (morning reset firing at 10:10 IST, mid-session).
  MACTZ="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"
  if [ "$MACTZ" != "Asia/Kolkata" ]; then
    echo "!! NOTE: Mac timezone is $MACTZ, not Asia/Kolkata. Tradewell's own jobs"
    echo "!! are timezone-proof since 08-Oct, but file dates and anything else on"
    echo "!! this Mac read wrong. Fix: System Settings -> General -> Date & Time."
  fi
  "$PY" "$ROOT/recorder/dataset.py" 2>&1
  echo
  "$PY" "$ROOT/recorder/analyze.py" 2>&1
  echo
  echo "=== NSE archives (participant OI + option EOD) ==================="
  "$PY" "$ROOT/recorder/nse_daily.py" 2>&1
  echo
  echo "=== Nightly learning pass ========================================"
  "$PY" "$ROOT/recorder/nightly_learn.py" 2>&1
  echo
  echo "=== Seller shadows (fade / theta windows / overnight decay) ======"
  "$PY" "$ROOT/recorder/seller_shadows.py" 2>&1
  echo
  echo "=== Gold lab (MCX + XAUUSD refresh + climatology) ================"
  "$PY" "$ROOT/recorder/mcx/nightly_gold.py" 2>&1
  echo
  echo "=== NIFTY climatology (3y habits, knowledge only) ================"
  "$PY" "$ROOT/recorder/nifty_climatology.py" 2>&1
} > "$OUT_DIR/$STAMP.txt"

# One-page visual dashboard of everything above — bookmarkable.
"$PY" "$ROOT/recorder/research_report.py" >> "$OUT_DIR/$STAMP.txt" 2>&1

# Deploy our static pages (canonical copies live in recorder/pages/) onto the
# app's public folder — git-excluded there, versioned here.
for f in "$ROOT"/recorder/pages/*.html; do
  [ -f "$f" ] && cp "$f" "$ROOT/frontend/public/$(basename "$f" | sed 's/_page//')"
done

ln -sf "$OUT_DIR/$STAMP.txt" "$OUT_DIR/latest.txt"

# Mirror recorder/data (candle DBs, ledgers, reconciled P&L — backed up
# nowhere else; the app's own nightly backup covers backend/ only) into the
# same iCloud folder the app uses. rsync = only changed files travel.
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/TradewellBackups/recorder-data"
mkdir -p "$ICLOUD" && rsync -a --delete "$ROOT/recorder/data/" "$ICLOUD/" 2>/dev/null \
  && echo "recorder/data mirrored to iCloud" >> "$OUT_DIR/$STAMP.txt"

# Safety net: anything committed in the research repo but not yet pushed
# goes to the personal GitHub mirror (niyasmk3/tradewellpersonal). Fail-soft:
# no network / no key = silent skip, the local repo is still the truth.
( cd "$ROOT/recorder" && git push -q origin main 2>/dev/null \
  && echo "research repo pushed to github" >> "$OUT_DIR/$STAMP.txt" ) || true

# Keep the app mirror current: after every pull from upstream, the app's main
# goes to the personal repo's app-mirror branch. Fail-soft like the above.
( git -C "$ROOT" push -q personal main:app-mirror 2>/dev/null \
  && echo "app mirror refreshed on github" >> "$OUT_DIR/$STAMP.txt" ) || true
