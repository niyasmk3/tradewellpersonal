"""Algo execution module — the only part of Tradewell that may place orders.

Everything else in this repo reads the market and hands a card to a human.
This package is the boundary crossing, so it is built the other way round from
the research modules: the strategy is the small part, and the gate in front of
it is the large part.

Plan: docs/algo-tab-plan-2026-09-16.md
"""
