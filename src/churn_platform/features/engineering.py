"""Feature engineering that travels inside the deployable model.

`FeatureEngineer` is the first step of every model pipeline. Because it is saved together
with the estimator, the served model computes derived features with exactly the code and
settings used in training: there is no second implementation in the API that could drift
(training-serving skew).

It also acts as a leakage guard: it selects the schema's feature columns by name, so an
accidentally passed label, identifier or extra column can never reach the estimator.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin

from churn_platform.data.schema import (
    BOOLEAN_FEATURES,
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    NUMERIC_FEATURES,
)

ENGINEERED_FEATURES = ("charge_per_usage_hour", "complaint_ratio", "payment_issues", "is_new_customer")
MODEL_NUMERIC_INPUTS = NUMERIC_FEATURES + BOOLEAN_FEATURES + ENGINEERED_FEATURES
MODEL_CATEGORICAL_INPUTS = CATEGORICAL_FEATURES


class FeatureEngineer(BaseEstimator, TransformerMixin):
    """Select schema features, normalise types and add derived features (stateless)."""

    def __init__(self, new_customer_months: int = 6):
        self.new_customer_months = new_customer_months

    def fit(self, X: pd.DataFrame, y=None) -> FeatureEngineer:
        missing = [c for c in FEATURE_COLUMNS if c not in X.columns]
        if missing:
            raise ValueError(f"missing feature columns: {missing}")
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in FEATURE_COLUMNS if c not in X.columns]
        if missing:
            raise ValueError(f"missing feature columns: {missing}")
        out = X.loc[:, list(FEATURE_COLUMNS)].copy()
        for column in CATEGORICAL_FEATURES:
            out[column] = out[column].astype(str)
        for column in BOOLEAN_FEATURES:
            out[column] = out[column].astype(float)
        for column in NUMERIC_FEATURES:
            out[column] = pd.to_numeric(out[column], errors="raise").astype(float)

        # Price relative to how much the customer actually uses the product.
        out["charge_per_usage_hour"] = out["monthly_charge"] / (out["monthly_usage_hours"] + 1.0)
        # Share of support contacts that were complaints (0 when there were no tickets).
        out["complaint_ratio"] = out["complaints_90d"] / np.maximum(out["support_tickets_90d"], 1.0)
        out["payment_issues"] = out["payment_failures_90d"] + out["late_payments_12m"]
        out["is_new_customer"] = (out["tenure_months"] < self.new_customer_months).astype(float)
        return out[list(MODEL_CATEGORICAL_INPUTS + MODEL_NUMERIC_INPUTS)]

    def get_feature_names_out(self, input_features=None) -> np.ndarray:
        return np.array(MODEL_CATEGORICAL_INPUTS + MODEL_NUMERIC_INPUTS, dtype=object)
