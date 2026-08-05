#!/usr/bin/env python3
"""Nightly learning pass — a fraction better than nothing, every evening.

Policy never changes here (that stays human, matching the owner's philosophy
and our PROGRAM.md). What DOES happen automatically, every close:

  1. All graded cards (train + locked-holdout counts only — holdout contents
     are never analyzed) are measured: which features separate wins from
     losses so far, and how well the engine's own score calibrates. Appended
     to learning_ledger.jsonl — the experiment's growing notebook.
  2. The Karpathy-harness ignition check: the moment the data gates pass
     (40+ train rows across 2+ months), this script LAUNCHES the baseline
     research run automatically and says so loudly. Until then it prints an
     honest countdown instead of silence.

Leakage rule respected: feature separation is computed on TRAIN rows only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LEDGER = DATA / "learning_ledger.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

IGNITION_MIN_ROWS = 40
IGNITION_MIN_MONTHS = 2


def main() -> int:
    import pandas as pd

    train_p, hold_p = DATA / "dataset.csv", DATA / "dataset_holdout.csv"
    train = pd.read_csv(train_p) if train_p.exists() else pd.DataFrame()
    n_hold = sum(1 for _ in open(hold_p)) - 1 if hold_p.exists() else 0
    n_train = len(train)
    total = n_train + max(n_hold, 0)

    entry = {
        "date": datetime.now(IST).strftime("%Y-%m-%d"),
        "graded_total": total,
        "train_rows": n_train,
        "holdout_rows": max(n_hold, 0),
    }

    if n_train >= 10 and "label_win" in train:
        num = train.select_dtypes("number").drop(
            columns=[c for c in ("label_win", "net_R", "net_pnl", "created_at") if c in train],
            errors="ignore",
        )
        wins, losses = train[train.label_win == 1], train[train.label_win == 0]
        seps = {}
        if len(wins) >= 3 and len(losses) >= 3:
            for c in num.columns:
                s = train[c].std()
                if s and s > 0:
                    seps[c] = round(float((wins[c].mean() - losses[c].mean()) / s), 3)
            top = sorted(seps.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
            entry["top_separating_features"] = dict(top)
        if "score_total" in train:
            entry["score_calibration"] = {
                "avg_score_wins": round(float(wins.score_total.mean()), 1) if len(wins) else None,
                "avg_score_losses": round(float(losses.score_total.mean()), 1) if len(losses) else None,
            }

    months = train["month"].nunique() if "month" in train and n_train else 0
    ignition = n_train >= IGNITION_MIN_ROWS and months >= IGNITION_MIN_MONTHS
    entry["ignition_ready"] = ignition

    with LEDGER.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")

    print(f"learning pass: {total} graded cards "
          f"({n_train} train / {entry['holdout_rows']} holdout)")
    if "top_separating_features" in entry:
        print(f"  separating features so far: {entry['top_separating_features']}")
    if "score_calibration" in entry:
        print(f"  score calibration: {entry['score_calibration']}")

    if ignition:
        already = (ROOT / "experiment" / "results.tsv").exists()
        print("  DATA GATES PASSED — launching baseline research run"
              + (" (results.tsv exists: continuing evidence)" if already else ""))
        r = subprocess.run(
            [sys.executable, str(ROOT / "experiment" / "harness.py"), "--run"],
            capture_output=True, text=True, timeout=600,
        )
        print("  " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "harness produced no output"))
    else:
        need_rows = max(0, IGNITION_MIN_ROWS - n_train)
        print(f"  research ignition: waiting ({need_rows} more train rows, "
              f"{months}/{IGNITION_MIN_MONTHS} months) — measuring meanwhile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
