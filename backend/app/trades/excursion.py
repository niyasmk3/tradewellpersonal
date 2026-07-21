"""Excursion analysis — how far trades actually run before they end.

THE QUESTION THIS EXISTS TO SETTLE: should Target 1 sit at +15% or +27%?

Lowering a target raises the hit rate but caps the winners that pay for the
losers, so it is not a free improvement — it moves the break-even win rate from
SL/(SL+T1) = 40% at 18/27 to about 55% at 18/15. Which side is right is an
empirical question about THIS engine's signals, and one or two trades cannot
answer it. Opinions (including mine) have been standing in for data.

MFE makes it answerable exactly, not approximately. The monitor records the best
premium seen only WHILE THE POSITION IS OPEN, so it stops at the moment the
trade ends. Therefore, for a row that stopped out, "MFE >= X%" means precisely
"a target at X% would have been filled BEFORE the stop was hit" — no inference
about ordering required, because a later excursion was never recorded.

Two honest limits:
  * MFE/MAE are sampled at the monitor's cadence (~5s), not tick by tick, so
    both understate the true extremes. Every conclusion here is therefore
    conservative in the same direction.
  * Replacing the target changes only the exit. It does not model the position
    slot freeing earlier, which could have allowed a different next trade.
"""
from __future__ import annotations

from app.trades.models import Trade, TradeStatus

# Candidate Target-1 levels, as a percentage gain on the entry premium.
_LEVELS = (5.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 27.0, 30.0, 40.0, 50.0)


def _pct(px: float | None, entry: float) -> float | None:
    if px is None or entry <= 0:
        return None
    return round((px / entry - 1) * 100, 2)


def rows(trades: list[Trade]) -> list[dict]:
    """Per-trade excursion, for closed rows that actually carry a measurement."""
    out = []
    for t in trades:
        if t.status is not TradeStatus.EXITED or not t.entry_premium:
            continue
        mfe, mae = _pct(t.mfe_premium, t.entry_premium), _pct(t.mae_premium, t.entry_premium)
        if mfe is None:
            continue                       # pre-dates the tracking; not a zero
        qty = t.initial_quantity or t.quantity
        out.append({
            "id": t.id, "contract": t.contract, "direction": t.direction.value,
            "entered_at": t.entered_at, "exited_at": t.exited_at,
            "entry": t.entry_premium, "exit": t.exit_premium,
            "mfe_pct": mfe, "mae_pct": mae,
            "realized_pct": _pct(t.exit_premium, t.entry_premium),
            "realized_pnl": t.realized_pnl, "quantity": qty,
            "reason": t.auto_close_reason,
        })
    return out


def target_curve(trades: list[Trade]) -> dict:
    """For each candidate target, what share of trades would have reached it.

    `expectancy_pct` re-prices each trade against that target: a row whose MFE
    reached the level exits there for +level; one that never reached it keeps
    the outcome it actually had. Gross of charges, which matter more the lower
    the target goes — a +5% target on a Rs 25 premium is largely eaten by them.
    """
    data = rows(trades)
    n = len(data)
    curve = []
    for lvl in _LEVELS:
        hits = [d for d in data if d["mfe_pct"] is not None and d["mfe_pct"] >= lvl]
        misses = [d for d in data if d not in hits]
        # A miss keeps whatever it actually returned; unknown exits count as 0.
        miss_sum = sum((d["realized_pct"] or 0.0) for d in misses)
        exp = ((len(hits) * lvl + miss_sum) / n) if n else 0.0
        curve.append({
            "target_pct": lvl,
            "reached": len(hits),
            "reached_rate": round(100 * len(hits) / n, 1) if n else 0.0,
            "expectancy_pct": round(exp, 2),
        })
    # With no measured trades every level scores 0.0 and max() would hand back
    # the first one — a confident "best target: 5%" derived from nothing.
    best = max(curve, key=lambda c: c["expectancy_pct"]) if n else None
    mfes = sorted(d["mfe_pct"] for d in data if d["mfe_pct"] is not None)
    maes = sorted(d["mae_pct"] for d in data if d["mae_pct"] is not None)

    def med(xs):
        return round(xs[len(xs) // 2], 2) if xs else None

    return {
        "trades": n,
        "curve": curve,
        "best_target_pct": best["target_pct"] if best else None,
        "median_mfe_pct": med(mfes),
        "median_mae_pct": med(maes),
        "worst_mae_pct": maes[0] if maes else None,
        # How much room a trade typically needs before it works — the same
        # measurement, pointed at the STOP question instead of the target.
        "mae_percentiles": {
            "p50": med(maes),
            "p80": round(maes[max(0, int(len(maes) * 0.2))], 2) if maes else None,
        },
        "note": (
            f"{n} closed trade(s) with excursion recorded. "
            "MFE is sampled every few seconds, not tick by tick, so it understates "
            "the true extreme. Under ~30 trades none of this is conclusive."
        ),
        "rows": sorted(data, key=lambda d: d["exited_at"] or 0, reverse=True),
    }
