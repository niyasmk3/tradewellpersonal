"""Exit-type expectancy: which way of ending a trade pays, and which bleeds.

The journal knows HOW each trade ended — plan trigger, broker fill, manual
click — and what it made. Grouping P&L by that exit type answers the question
the daily forensics kept re-deriving by hand: are the stops earning their
keep, is "broker flat while the engine said hold" a leak, do stall exits
actually save money. Expectancy per type, not per trade overall, because the
overall number averages away exactly the distinction that matters.

Capture efficiency is the second axis: realised P&L divided by the best the
position ever offered ((MFE − entry) × size). A 40% capture on winning exits
means most of the move was given back — a target/trail problem, not a signal
problem. Only measured where MFE tracking covered the trade FROM ENTRY and
the trade went positive at some point; anything else would be fiction.

P&L basis: the store books realized_pnl GROSS of charges, which matches the
live journal's own convention — but the paper book's summary is net, and "at
scalp cadence the charges ARE the game". So the caller chooses: pass
`charges_fn` (trade -> round-trip cost) to report net, and the payload says
which basis it used either way. The paper route passes the Zerodha model;
serving the paper book gross here would let a book /paper/summary calls
negative read as positive expectancy — the exact number that gates
SCALP_LIVE_ENABLED.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from app.trades.models import Trade, TradeStatus

# Reasons the monitor/reconciler stamp on auto-closed rows, normalised to
# journal-stable keys. Anything user-supplied lands under its own name.
_REASON_KEYS = {"broker flat": "broker_flat"}


def exit_type(t: Trade) -> str:
    """Stable grouping key for how this trade ended."""
    if t.auto_closed and t.auto_close_reason:
        return _REASON_KEYS.get(t.auto_close_reason, t.auto_close_reason)
    return getattr(t, "exit_reason", None) or "manual"


def _capture(t: Trade) -> float | None:
    """realised / best-offered, or None when the ratio has no meaning.

    Same clean-sample rule as calibration._p75_mfe_pct: tracking must have
    covered the trade from entry (within one monitor cadence). An MFE that
    started mid-life understates the potential, which INFLATES the ratio —
    a give-back leak reading as near-perfect capture is worse than a blank.
    Gross on both sides on purpose, so the ratio measures the exit, not fees.
    """
    if t.mfe_premium is None or t.entry_premium is None:
        return None
    if t.excursion_from is None or t.excursion_from - t.entered_at > 30:
        return None
    qty = t.initial_quantity or t.quantity
    potential = (t.mfe_premium - t.entry_premium) * qty
    if potential <= 0 or not qty:
        return None                      # never went positive: nothing to capture
    return max(min(t.realized_pnl / potential, 1.0), -5.0)


def summarize(trades: Iterable[Trade],
              charges_fn: Optional[Callable[[Trade], float]] = None) -> dict:
    closed = [t for t in trades
              if t.status is TradeStatus.EXITED and t.exited_at is not None]

    def pnl(t: Trade) -> float:
        return round(t.realized_pnl - charges_fn(t), 2) if charges_fn else t.realized_pnl

    groups: dict[str, list[Trade]] = {}
    for t in closed:
        groups.setdefault(exit_type(t), []).append(t)

    def _stats(rows: list[Trade]) -> dict:
        pnls = [pnl(t) for t in rows]
        wins = [p for p in pnls if p > 0]
        return {
            "n": len(rows),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(rows), 2),
            "total_pnl": round(sum(pnls), 2),
            "expectancy": round(sum(pnls) / len(rows), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(p for p in pnls if p <= 0) / max(len(pnls) - len(wins), 1), 2)
                        if len(wins) < len(pnls) else None,
        }

    by_type = []
    for key, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        captures = [c for c in (_capture(t) for t in rows) if c is not None]
        entry = {"exit_type": key, **_stats(rows)}
        entry["capture_pct"] = (round(sum(captures) / len(captures) * 100, 1)
                                if captures else None)
        # The trader's own answers, sub-grouped — this is what the exit-reason
        # prompt exists to feed. "broker flat" alone cannot distinguish a
        # disciplined broker-side stop from a fear exit; the recorded reasons
        # can, and each gets its own expectancy so the two stop being blended.
        reasons = {getattr(t, "exit_reason", None) for t in rows}
        if any(reasons - {None}):
            entry["by_reason"] = {
                (r or "unexplained"): _stats([t for t in rows
                                              if getattr(t, "exit_reason", None) == r])
                for r in sorted(reasons, key=lambda x: (x is None, x or ""))
            }
        by_type.append(entry)

    # Rows that ended at the broker while the engine still said HOLD and the
    # user never said why. Until a reason lands, that P&L is unclassifiable —
    # it could be discipline (a real stop at the broker) or the fear-exit leak
    # the 22-Jul forensics found. The UI prompts on exactly this list.
    needs_reason = [t.id for t in closed
                    if t.auto_closed and t.auto_close_reason == "broker flat"
                    and not getattr(t, "exit_reason", None)]

    return {
        "n_closed": len(closed),
        "total_pnl": round(sum(pnl(t) for t in closed), 2),
        # Which side of the charges the numbers sit on — never leave it implied.
        "pnl_basis": "net_of_charges" if charges_fn else "gross_of_charges",
        "by_exit_type": by_type,
        "needs_reason": needs_reason,
    }
