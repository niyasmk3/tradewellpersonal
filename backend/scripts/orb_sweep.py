"""ORB parameter sweep — rerun the opening-range evidence on demand.

The 22-Jul standalone run graded ORB over 58 sessions and every configuration
lost money; that evidence is why the 09:15 warm-up window stays signal-free.
Evidence goes stale, though — this script re-runs the same grid against fresh
history so the "no open trading" decision keeps earning its place instead of
fossilising.

Usage (from backend/, with a valid Kite session in .kite_session.json and the
Historical Data add-on on the account):

    .venv/bin/python scripts/orb_sweep.py --symbol NIFTY --days 60
    .venv/bin/python scripts/orb_sweep.py --days 90 --timeframe 5m --json out.json

Honest scope note: this is an IN-SAMPLE sweep, and stays one on purpose. A
walk-forward harness (fit each parameter on trailing months, grade on the next)
is the right tool once any config here shows positive expectancy worth
protecting from overfit — building it while every row is negative would be
rigor spent proving a harder version of "no". Revisit if a row goes green
across two consecutive sweeps.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ on the path

from app.backtest import data
from app.backtest.orb import OrbParams, run_orb
from app.config import get_settings
from app.kite.client import kite_service
from app.kite.instruments import resolve_universe
from app.state import market_state

_IST = timezone(timedelta(hours=5, minutes=30))

# The grid the 22-Jul run graded, kept verbatim so sweeps stay comparable
# across time. `fade` is a diagnostic (does the open revert?), not a strategy.
CONFIGS: list[tuple[str, OrbParams]] = [
    ("ORB15 rr1.5 (base)",   OrbParams(or_minutes=15, rr=1.5)),
    ("ORB15 rr1.0",          OrbParams(or_minutes=15, rr=1.0)),
    ("ORB30 rr1.5",          OrbParams(or_minutes=30, rr=1.5)),
    ("ORB15 stop-mid",       OrbParams(or_minutes=15, rr=1.5, stop_at_mid=True)),
    ("ORB15 fade (diag)",    OrbParams(or_minutes=15, rr=1.0, fade=True)),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep ORB configs over recent history")
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--timeframe", default="3m", choices=["1m", "3m", "5m", "15m"])
    ap.add_argument("--json", help="also write full results to this path")
    args = ap.parse_args()

    if not kite_service.is_authenticated or kite_service.kite is None:
        print("No Kite session — log in via the dashboard first (this script "
              "reads the cached token).", file=sys.stderr)
        return 1

    settings = get_settings()
    resolve_universe(kite_service.kite, settings, market_state)
    meta = market_state.underlyings.get(args.symbol.upper())
    if meta is None or not meta.fut_token:
        print(f"No futures instrument resolved for {args.symbol}", file=sys.stderr)
        return 1

    to_dt = datetime.now(_IST)
    from_dt = to_dt - timedelta(days=args.days)
    print(f"Fetching {args.timeframe} candles for {args.symbol} fut "
          f"({from_dt.date()} → {to_dt.date()})…")
    df = data.fetch_futures(kite_service.kite, meta.fut_token, from_dt, to_dt, args.timeframe)
    if df.empty:
        print("No candles returned — Historical add-on missing or range empty.",
              file=sys.stderr)
        return 1
    sessions = df["ts"].map(lambda t: (int(t) + 19800) // 86400).nunique()
    print(f"{len(df)} bars across {sessions} sessions\n")

    header = f"{'config':<22}{'trades':>7}{'WR%':>7}{'expR':>8}{'totR':>8}{'maxDD':>8}{'PF':>7}"
    print(header)
    print("-" * len(header))
    results = []
    for name, params in CONFIGS:
        r = run_orb(df, params, symbol=args.symbol.upper())
        pf = f"{r.profit_factor:.2f}" if r.profit_factor is not None else "—"
        print(f"{name:<22}{r.trades_total:>7}{r.win_rate:>7.1f}{r.expectancy_r:>8.3f}"
              f"{r.total_r:>8.2f}{r.max_drawdown_r:>8.2f}{pf:>7}")
        results.append({"config": name, "params": vars(params),
                        "result": r.model_dump(exclude={"equity_curve", "trades"})})

    print("\nGraded on the underlying future: theta, IV and option spreads are "
          "excluded, so real option results would be WORSE than every row above.")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"generated_at": int(to_dt.timestamp()), "symbol": args.symbol.upper(),
             "days": args.days, "timeframe": args.timeframe, "sessions": sessions,
             "results": results}, indent=2))
        print(f"Full results written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
