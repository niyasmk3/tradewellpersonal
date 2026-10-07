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
mkdir -p "$OUT_DIR"

{
  echo "Tradewell evening report — $STAMP"
  echo "======================================================"
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
