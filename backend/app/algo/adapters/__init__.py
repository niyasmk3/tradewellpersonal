"""Strategy adapters — thin, one per strategy, each turning that strategy's
existing card into an OrderIntent. They contain NO trading rules: the rule
lives where it always did (closing/tonight.py, gold/rules.py), the adapter
only reads its verdict and says what order that verdict implies.
"""
