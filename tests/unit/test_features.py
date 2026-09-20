"""Feature engineering and the shared model pipeline: derived values, leakage guard, stable layout."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from churn_platform.data.schema import FEATURE_COLUMNS, LABEL_COLUMN
from churn_platform.features.engineering import FeatureEngineer
from churn_platform.features.preprocessing import build_model_pipeline
from churn_platform.training.algorithms import build_classifier
from tests.support.data import small_dataset


def customer(**overrides) -> pd.DataFrame:
    row = {
        "plan_tier": "standard",
        "contract_type": "monthly",
        "autopay_enabled": True,
        "recent_price_increase": False,
        "tenure_months": 5,
        "monthly_charge": 39.0,
        "payment_failures_90d": 1,
        "late_payments_12m": 2,
        "monthly_usage_hours": 12.0,
        "sessions_30d": 10,
        "days_since_last_login": 3,
        "usage_trend_pct": -0.1,
        "support_tickets_90d": 0,
        "complaints_90d": 0,
        "avg_resolution_hours": None,
        "features_adopted": 4,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_derived_features_are_computed_from_snapshot_values():
    out = FeatureEngineer(new_customer_months=6).fit_transform(customer(support_tickets_90d=4, complaints_90d=1))

    assert out.loc[0, "charge_per_usage_hour"] == pytest.approx(39.0 / 13.0)
    assert out.loc[0, "complaint_ratio"] == pytest.approx(0.25)
    assert out.loc[0, "payment_issues"] == 3
    assert out.loc[0, "is_new_customer"] == 1.0


def test_complaint_ratio_is_zero_without_tickets():
    out = FeatureEngineer().fit_transform(customer(support_tickets_90d=0, complaints_90d=0))

    assert out.loc[0, "complaint_ratio"] == 0.0


@pytest.mark.parametrize(("tenure", "expected"), [(5, 1.0), (6, 0.0), (48, 0.0)])
def test_new_customer_boundary_follows_configuration(tenure, expected):
    out = FeatureEngineer(new_customer_months=6).fit_transform(customer(tenure_months=tenure))

    assert out.loc[0, "is_new_customer"] == expected


def test_missing_feature_column_is_rejected():
    with pytest.raises(ValueError, match="missing feature columns"):
        FeatureEngineer().fit_transform(customer().drop(columns=["monthly_charge"]))


@pytest.fixture(scope="module")
def fitted_pipeline():
    data = small_dataset(rows=3000)
    pipeline = build_model_pipeline(build_classifier("logistic_regression", {"max_iter": 500}, 0), 6)
    pipeline.fit(data.loc[:, list(FEATURE_COLUMNS)], data[LABEL_COLUMN])
    return pipeline, data


def test_label_and_extra_columns_cannot_reach_the_estimator(fitted_pipeline):
    # The feature step selects schema columns by name, so passing the label (or ids) along
    # with the features must not change a single prediction.
    pipeline, data = fitted_pipeline
    features_only = pipeline.predict_proba(data.loc[:, list(FEATURE_COLUMNS)])
    with_leaky_columns = pipeline.predict_proba(data.assign(cancellation_status="x"))

    np.testing.assert_array_equal(features_only, with_leaky_columns)


def test_column_order_of_the_input_does_not_matter(fitted_pipeline):
    pipeline, data = fitted_pipeline
    shuffled = data.loc[:, list(reversed(FEATURE_COLUMNS))]

    np.testing.assert_array_equal(
        pipeline.predict_proba(data.loc[:, list(FEATURE_COLUMNS)]), pipeline.predict_proba(shuffled)
    )


def test_unknown_category_is_refused_instead_of_silently_scored(fitted_pipeline):
    pipeline, _ = fitted_pipeline

    with pytest.raises(ValueError):
        pipeline.predict_proba(customer(plan_tier="platinum"))
