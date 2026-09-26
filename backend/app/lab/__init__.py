"""Strategy Lab — 40-strategy NIFTY F&O research + paper-trading platform.

Everything in this package is research or simulation. There is no order path:
the only route to a broker in this repo is app/algo, behind its own ladder.

Sub-modules:
    models      shared dataclasses (params, signals, legs, fills, positions)
    catalog     the 40 strategy definitions + parameter schemas
    store       sqlite persistence (.lab.db) — versions, runs, ledgers, experiments
    data        historical sources + readiness + quality checks
    costs       date-aware Indian F&O transaction-cost model (futures + options)
    engine      event-driven backtester (one code path, honest fills)
    metrics     performance statistics with small-sample warnings
    regime      six-bucket market-regime classifier (causal, per bar)
    strategies  the implementations, one module per category
    risk        shared portfolio risk engine (caps, emergency stop)
    paper       daily automated paper-trading loop + session jobs
    learning    champion/challenger experiments, walk-forward validation
    report      daily learning report builder
    service     orchestration for the API routes
"""
