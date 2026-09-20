"""Monitoring building blocks: dataset construction, drift/quality/performance summaries and
the delayed-outcome rule. Evidently itself is exercised by the integration tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pandas as pd
import pytest

from churn_platform.config import load_monitoring_config
from churn_platform.data.schema import FEATURE_COLUMNS
from churn_platform.monitoring.analysis import (
    drift_scores,
    missing_shares,
    summarize_drift,
    summarize_performance,
    summarize_quality,
)
from churn_platform.monitoring.datasets import observations_frame
from churn_platform.simulation.outcomes import outcome_is_resolvable
from tests.support.data import REPO_CONFIG, small_dataset
from tests.support.serving import valid_payload

CONFIG = load_monitoring_config(REPO_CONFIG)


def db_row(minutes: int, **feature_overrides):
    features = valid_payload(**feature_overrides)["features"]
    predicted_at = datetime(2026, 3, 1, tzinfo=UTC) + timedelta(minutes=minutes)
    return (uuid4(), predicted_at, f"C-{minutes}", date(2026, 2, 1), 0.42, 0, None, features)


def test_observations_become_a_typed_feature_frame_in_time_order():
    frame = observations_frame([db_row(5), db_row(1, support_tickets_90d=0, complaints_90d=0, avg_resolution_hours=None)])

    assert list(frame["customer_id"]) == ["C-1", "C-5"]
    assert set(FEATURE_COLUMNS) <= set(frame.columns)
    assert frame["autopay_enabled"].dtype == bool
    assert frame["tenure_months"].dtype == "int64" and frame["monthly_charge"].dtype == "float64"
    assert pd.isna(frame.loc[0, "avg_resolution_hours"])


def test_no_observations_give_an_empty_frame_with_the_expected_columns():
    frame = observations_frame([])

    assert frame.empty and set(FEATURE_COLUMNS) <= set(frame.columns)


def value_drift(column, value):
    return {"metric_name": f"ValueDrift(column={column})", "config": {"type": "evidently:metric_v2:ValueDrift", "column": column}, "value": value}


def test_drift_scores_are_read_from_evidently_value_drift_metrics_only():
    metrics = [value_drift("tenure_months", 0.2), {"config": {"type": "evidently:metric_v2:RowCount"}, "value": 10}]

    assert drift_scores(metrics) == {"tenure_months": 0.2}


def test_missing_shares_are_read_per_column():
    metrics = [
        {"config": {"type": "evidently:metric_v2:MissingValueCount", "column": "avg_resolution_hours"}, "value": {"count": 5, "share": 0.5}},
        {"config": {"type": "evidently:metric_v2:DatasetMissingValueCount"}, "value": {"count": 5, "share": 0.1}},
    ]

    assert missing_shares(metrics) == {"avg_resolution_hours": 0.5}


def scores(drifted: int, prediction: float = 0.0) -> dict[str, float]:
    values = {column: (0.5 if i < drifted else 0.01) for i, column in enumerate(FEATURE_COLUMNS)}
    return {**values, "churn_probability": prediction}


def test_feature_at_the_threshold_counts_as_drifted():
    summary = summarize_drift({**scores(0), "tenure_months": CONFIG.drift.feature_threshold}, CONFIG.drift)

    assert summary.drifted_features == ["tenure_months"]


@pytest.mark.parametrize(("drifted", "dataset_drift"), [(3, False), (4, True), (8, True)])
def test_dataset_drift_depends_on_the_share_of_drifted_features(drifted, dataset_drift):
    summary = summarize_drift(scores(drifted), CONFIG.drift)

    assert summary.drift_share == pytest.approx(drifted / len(FEATURE_COLUMNS))
    assert summary.dataset_drift is dataset_drift


def test_prediction_drift_is_judged_separately_from_feature_drift():
    summary = summarize_drift(scores(0, prediction=0.3), CONFIG.drift)

    assert summary.prediction_drift and not summary.dataset_drift


def test_rising_missing_values_and_duplicates_are_data_quality_issues():
    reference = small_dataset(rows=500, seed=4)
    current = small_dataset(rows=200, seed=5)
    current = pd.concat([current, current.head(30)])  # the same snapshots predicted twice

    summary = summarize_quality(current, {"monthly_usage_hours": 0.2}, reference, CONFIG.data_quality)

    assert not summary.ok
    assert any("monthly_usage_hours" in issue for issue in summary.issues)
    assert any("duplicate" in issue for issue in summary.issues)


def test_structural_missing_values_are_not_a_quality_issue():
    # avg_resolution_hours is empty exactly when there were no tickets: a change in that
    # share reflects fewer tickets (drift), not broken data.
    reference = small_dataset(rows=500, seed=4)
    current = small_dataset(rows=200, seed=5)

    assert summarize_quality(current, {"avg_resolution_hours": 0.95}, reference, CONFIG.data_quality).ok


def test_clean_window_has_no_quality_issues():
    reference = small_dataset(rows=500, seed=4)
    current = small_dataset(rows=200, seed=5)
    reference_missing = float(reference["avg_resolution_hours"].isna().mean())

    assert summarize_quality(current, {"avg_resolution_hours": reference_missing}, reference, CONFIG.data_quality).ok


def labeled(n, correct_share=1.0):
    actual = [1, 0] * (n // 2)
    cut = int(len(actual) * correct_share)
    predicted = actual[:cut] + [1 - a for a in actual[cut:]]
    return pd.DataFrame({"actual_churn": actual, "churn_prediction": predicted, "churn_probability": [0.9 if p else 0.1 for p in predicted]})


def test_performance_waits_for_enough_resolved_outcomes():
    summary = summarize_performance(labeled(100), {"f1": 0.5, "recall": 0.6}, min_labeled=200)

    assert summary.metrics is None and summary.f1_drop is None and summary.labeled_count == 100


def test_performance_drop_is_measured_against_the_models_test_metrics():
    summary = summarize_performance(labeled(400, correct_share=0.5), {"f1": 0.9, "recall": 0.9}, min_labeled=200)

    assert summary.metrics["f1"] == pytest.approx(0.5)
    assert summary.f1_drop == pytest.approx(0.4)


@pytest.mark.parametrize(("days_after_snapshot", "resolvable"), [(29, False), (30, True), (45, True)])
def test_outcome_is_known_only_after_the_full_window(days_after_snapshot, resolvable):
    snapshot = date(2026, 3, 1)

    assert outcome_is_resolvable(snapshot, snapshot + timedelta(days=days_after_snapshot), 30) is resolvable
