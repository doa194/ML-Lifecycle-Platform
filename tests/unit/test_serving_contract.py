"""Training-serving consistency and risk classification.

The API contract must describe exactly the model's input space, and a customer sent
through the API conversion must be scored exactly like the same row in the training data.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from churn_platform.data.schema import FEATURE_COLUMNS, NUMERIC_RANGES
from churn_platform.serving.risk import classify_risk
from churn_platform.serving.schemas import CustomerFeatures, to_frame
from tests.support.data import small_dataset
from tests.support.serving import trained_model


def test_api_contract_covers_exactly_the_model_features_with_training_ranges():
    fields = CustomerFeatures.model_fields
    schema = CustomerFeatures.model_json_schema()["properties"]

    assert set(fields) == set(FEATURE_COLUMNS)
    for column, (low, high) in NUMERIC_RANGES.items():
        bounds = schema[column].get("anyOf", [schema[column]])[0]
        assert (bounds["minimum"], bounds["maximum"]) == (low, high), column


def test_rows_sent_through_the_api_conversion_score_like_training_rows():
    rows = small_dataset(rows=400, seed=31).head(200)
    model = trained_model()

    direct = model.predict_proba(rows)
    records = rows.loc[:, list(FEATURE_COLUMNS)].to_dict(orient="records")
    via_api = np.array(
        [
            model.predict_proba(
                to_frame(CustomerFeatures.model_validate({k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in r.items()}))
            )[0]
            for r in records
        ]
    )

    np.testing.assert_allclose(via_api, direct, rtol=0, atol=1e-12)


@pytest.mark.parametrize(
    ("probability", "expected"),
    [(0.80, "high"), (0.50, "high"), (0.49, "medium"), (0.30, "medium"), (0.29, "low"), (0.0, "low")],
)
def test_risk_levels_follow_the_model_threshold(probability, expected):
    assert classify_risk(probability, decision_threshold=0.50, medium_ratio=0.6) == expected
