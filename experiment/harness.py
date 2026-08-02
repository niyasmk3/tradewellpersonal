#!/usr/bin/env python3
"""Experiment harness — the fixed evaluation autoresearch's prepare.py is.

The agent edits `experiment.py` ONLY. This file is the ground truth and is
never modified during a research run: it loads the dataset, builds expanding
walk-forward folds by calendar month, scores the experiment's model, and
appends every attempt to results.tsv. The keep/discard verdict is mechanical
(PROGRAM.md documents the rule) so metric-chasing can't argue with it.

Commands:
  harness.py --run       evaluate current experiment.py on dataset.csv
  harness.py --selftest  synthetic end-to-end proof of the whole loop
  harness.py --final     ONE-SHOT holdout evaluation (guarded; end of a
                         research run only — see PROGRAM.md)

Metric: mean out-of-sample expectancy uplift — mean net_R of trades the model
would take (p >= THRESHOLD) minus mean net_R of all trades, averaged across
folds. A filter that keeps almost nothing proves nothing, so retention is
part of the verdict.

KEEP rule (all must hold):
  mean uplift >= 0.05 R      positive uplift in >= 2/3 of folds
  mean retention >= 40%      total test trades across folds >= 30
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
RESULTS = HERE / "results.tsv"
RESULTS_FINAL = HERE / "results_final.tsv"

MIN_TRAIN, MIN_TEST = 40, 8
KEEP_UPLIFT, KEEP_POS_FRAC, KEEP_RETAIN, KEEP_MIN_TRADES = 0.05, 2 / 3, 0.40, 30


def log(msg: str) -> None:
    print(msg, flush=True)


def load_experiment():
    spec = importlib.util.spec_from_file_location("experiment", HERE / "experiment.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git_head() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(HERE.parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


_warned_missing = False


def prep_xy(df, features: list[str], categoricals: list[str]):
    import pandas as pd

    global _warned_missing
    present = [f for f in features if f in df.columns]
    missing = sorted(set(features) - set(present))
    if missing and not _warned_missing:
        log(f"  note: features absent from dataset, skipped: {missing}")
        _warned_missing = True
    X = df[present].copy()
    cats = [c for c in categoricals if c in X.columns]
    if cats:
        X = pd.get_dummies(X, columns=cats, dummy_na=True)
    X = X.apply(pd.to_numeric, errors="coerce")
    y = df["label_win"].astype(int)
    return X, y


def walk_forward_folds(df):
    months = sorted(df["month"].dropna().unique())
    folds = []
    for i in range(1, len(months)):
        train = df[df["month"].isin(months[:i])]
        test = df[df["month"] == months[i]]
        if len(train) >= MIN_TRAIN and len(test) >= MIN_TEST:
            folds.append((months[i], train, test))
    return folds


def evaluate(df, exp) -> dict:
    import numpy as np

    df = df.dropna(subset=["net_R", "label_win", "month"]).reset_index(drop=True)
    folds = walk_forward_folds(df)
    if len(folds) < 2:
        return {"error": f"not enough data: {len(df)} usable rows, {len(folds)} usable folds (need 2+)"}

    uplifts, retains, n_test = [], [], 0
    for month, train, test in folds:
        X_tr, y_tr = prep_xy(train, exp.FEATURES, exp.CATEGORICALS)
        X_te, _ = prep_xy(test, exp.FEATURES, exp.CATEGORICALS)
        X_te = X_te.reindex(columns=X_tr.columns, fill_value=0)
        model = exp.build_model()
        model.fit(X_tr, y_tr)
        p = model.predict_proba(X_te)[:, 1]
        taken = test["net_R"].values[p >= exp.THRESHOLD]
        base = float(test["net_R"].mean())
        up = float(np.mean(taken) - base) if len(taken) else float("nan")
        uplifts.append(up)
        retains.append(len(taken) / len(test))
        n_test += len(test)
        log(f"  fold {month}: base_R {base:+.3f}  taken {len(taken)}/{len(test)}"
            f"  uplift {up:+.3f}" if len(taken) else
            f"  fold {month}: base_R {base:+.3f}  taken 0/{len(test)}  uplift n/a")

    import math
    clean = [u for u in uplifts if not math.isnan(u)]
    mean_up = sum(clean) / len(clean) if clean else float("nan")
    pos_frac = sum(1 for u in clean if u > 0) / len(uplifts) if uplifts else 0.0
    mean_ret = sum(retains) / len(retains)
    verdict = (
        "PASS" if clean and mean_up >= KEEP_UPLIFT and pos_frac >= KEEP_POS_FRAC
        and mean_ret >= KEEP_RETAIN and n_test >= KEEP_MIN_TRADES else "FAIL"
    )
    return {"mean_uplift": mean_up, "pos_frac": pos_frac, "retention": mean_ret,
            "n_test": n_test, "folds": len(folds), "verdict": verdict}


def append_results(path: Path, exp, m: dict) -> None:
    new = not path.exists()
    with path.open("a") as fh:
        if new:
            fh.write("ts\tcommit\tverdict\tmean_uplift\tpos_folds\tretention\tn_test\tthreshold\tdescription\n")
        fh.write(
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t{git_head()}\t"
            f"{m.get('verdict', 'CRASH')}\t{m.get('mean_uplift', float('nan')):.4f}\t"
            f"{m.get('pos_frac', 0):.2f}\t{m.get('retention', 0):.2f}\t{m.get('n_test', 0)}\t"
            f"{exp.THRESHOLD}\t{exp.DESCRIPTION}\n"
        )


def run(csv: Path, exp, results_path: Path | None) -> int:
    import pandas as pd

    if not csv.exists():
        log(f"{csv.name} missing — run recorder/dataset.py first"); return 1
    df = pd.read_csv(csv)
    log(f"{csv.name}: {len(df)} rows | experiment: {exp.DESCRIPTION}")
    m = evaluate(df, exp)
    if "error" in m:
        log(f"ABORT: {m['error']}"); return 1
    log(f"mean uplift {m['mean_uplift']:+.3f} R | positive folds {m['pos_frac']:.0%} | "
        f"retention {m['retention']:.0%} | test trades {m['n_test']} | "
        f"VERDICT: {m['verdict']}")
    if results_path is not None:
        append_results(results_path, exp, m)
        log(f"logged to {results_path.name}")
    return 0


def selftest() -> int:
    """Synthetic 9-month dataset with a planted weak edge; proves mechanics."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    rows = []
    for mi in range(9):
        month = f"2026-{mi + 1:02d}"
        for _ in range(30):
            score = rng.uniform(60, 95)
            oi = rng.uniform(4, 20)
            p_win = 1 / (1 + np.exp(-(0.06 * (score - 75) + 0.10 * (oi - 12))))
            win = rng.random() < p_win
            net_r = rng.normal(1.2, 0.4) if win else rng.normal(-1.0, 0.2)
            rows.append({
                "created_at": 1750000000 + mi * 2_600_000,
                "month": month, "mode": rng.choice(["intraday", "positional"]),
                "direction": rng.choice(["CE", "PE"]),
                "score_total": score, "comp_options_oi": oi,
                "rsi": rng.uniform(30, 80), "hour_ist": rng.uniform(9.25, 15.5),
                "label_win": int(win), "net_R": round(float(net_r), 3),
            })
    df = pd.DataFrame(rows)
    exp = load_experiment()
    m = evaluate(df, exp)
    assert "error" not in m, m
    assert m["folds"] >= 2 and m["n_test"] > 100
    log(f"selftest OK — {m['folds']} folds, {m['n_test']} test trades, "
        f"uplift {m['mean_uplift']:+.3f}, verdict {m['verdict']} "
        "(planted edge should usually PASS)")
    return 0


def final() -> int:
    if not RESULTS.exists() or sum(1 for _ in RESULTS.open()) < 6:
        log("REFUSED: --final is the END of a research run. Run and log at "
            "least 5 experiments on dataset.csv first (results.tsv proves it).")
        return 1
    log("=" * 68)
    log("FINAL HOLDOUT EVALUATION — this is a one-shot answer, not a metric")
    log("to iterate against. If you edit experiment.py after seeing this,")
    log("the holdout is burned and only NEW data can grade you again.")
    log("=" * 68)
    return run(DATA / "dataset_holdout.csv", load_experiment(), RESULTS_FINAL)


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--final" in sys.argv:
        return final()
    return run(DATA / "dataset.csv", load_experiment(), RESULTS)


if __name__ == "__main__":
    sys.exit(main())
