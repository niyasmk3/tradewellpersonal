"""Overnight — the confirmed variant of the Closing Day strategy.

Same trade as app/closing (ATM option at 15:05, sold at the next 09:50), one
extra condition: the day's body (15:00 vs today's open) and the last hour
(15:00 vs 14:00) must AGREE on a direction. A disagreement night is a
stand-aside, not a fade — "follow the last hour when they disagree" scored
negative skill in every window tested (docs/closing-day-strategy-2026-08-19.md,
Round 3).

Deliberately a separate module and tab rather than a flag on Closing Day: the
study tab stays the unfiltered hypothesis record, and this surface owns the
filtered strategy — each with its own stored results, so re-running one never
rewrites the other's evidence.

Owns no data. The price spine is the Patterns store, VIX is the Closing store,
and the trades themselves come from app/closing/study.py run unfiltered and
PARTITIONED here by the agreement test — mathematically identical to filtering
inside the backtest (each night is independent), and it means the skipped
nights' P&L is computed too, which is the evidence the filter earns its keep.
"""
