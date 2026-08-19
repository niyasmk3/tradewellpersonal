"""THE file the research agent edits — everything else is ground truth.

Change FEATURES, CATEGORICALS, THRESHOLD, and build_model() freely. Keep
DESCRIPTION honest and one line; it becomes the results.tsv row. The harness
ignores any feature name absent from the dataset (logged), so speculative
features are safe to list.
"""

DESCRIPTION = "baseline: logistic regression on card-score + time features"

FEATURES = [
    "score_total",
    "comp_price_action",
    "comp_trend",
    "comp_volume",
    "comp_volatility",
    "comp_options_oi",
    "comp_news",
    "risk_reward",
    "inval_dist_pct",
    "hour_ist",
    "toxic_window",
    "dow",
    "rsi",
    "adx",
    "atr_pct",
    "vwap_dist_pct",
    "pcr",
    "oi_change_skew",
    "vix_close",
    "advocate_counter",
    "tape_resolved_pct",
    "tape_aligned",
    "tape_state",
    "mode",
    "direction",
]

CATEGORICALS = ["mode", "direction", "tape_state"]

THRESHOLD = 0.55


def build_model():
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("clf", LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000)),
    ])
