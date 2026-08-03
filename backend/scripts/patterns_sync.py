"""CLI for the Patterns Module: fetch 3y of NIFTY 5-min data and/or analyze.

Run from backend/ with the venv python (needs today's Kite session for sync):
    python scripts/patterns_sync.py            # sync + analyze
    python scripts/patterns_sync.py --no-sync  # analyze only (offline)
    python scripts/patterns_sync.py --years 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--no-sync", action="store_true", help="skip Kite fetch, analyze stored data")
    ap.add_argument("--no-analyze", action="store_true", help="fetch only")
    args = ap.parse_args()

    from app.patterns.service import PatternsError, run_analysis, run_sync

    if not args.no_sync:
        from app.kite.client import kite_service

        if not kite_service.is_authenticated:
            print("No valid Kite session for today — log in via the app first, then rerun.")
            return 1
        try:
            summary = run_sync(kite_service.kite, years=args.years)
        except Exception as exc:
            print(f"Sync failed: {exc}")
            return 1
        print(json.dumps(summary, indent=2))

    if not args.no_analyze:
        try:
            results = run_analysis()
        except PatternsError as exc:
            print(f"Analysis failed: {exc}")
            return 1
        d = results["data"]
        print(f"Analyzed {d['bars']} bars / {d['days']} days ({d['from']} -> {d['to']}).")
        print("Results saved to backend/.patterns_results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
