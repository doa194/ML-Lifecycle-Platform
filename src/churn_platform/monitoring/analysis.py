"""Evidently analysis of reference vs. current data, reduced to the facts the policy needs.

Evidently computes the statistics (per-feature and prediction drift distances, missing
values and a full data summary) and renders the HTML report kept in MLflow. This module
turns its metric list into small, typed summaries:
  * DriftSummary       - which features drifted, dataset-level drift, prediction drift
  * DataQualitySummary - missing-value increases and duplicate snapshots
  * PerformanceSummary - delayed metrics once outcomes are known (same metric code as the
                         training evaluation, so production and test numbers are comparable)
"""

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass, field

import pandas as pd

from churn_platform.config import DataQualityConfig, DriftConfig
from churn_platform.data.schema import (
    BOOLEAN_FEATURES,
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    NULLABLE_FEATURES,
    NUMERIC_FEATURES,
)
from churn_platform.training.metrics import metrics_for_predictions

PREDICTION_COLUMN = "churn_probability"


@dataclass(frozen=True)
class DriftSummary:
    feature_scores: dict[str, float]
    drifted_features: list[str]
    drift_share: float
    dataset_drift: bool
    prediction_drift_score: float
    prediction_drift: bool


@dataclass(frozen=True)
class DataQualitySummary:
    rows: int
    duplicate_share: float
    missing_share: dict[str, float]
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}


@dataclass(frozen=True)
class PerformanceSummary:
    labeled_count: int
    metrics: dict | None
    reference: dict
    f1_drop: float | None
    recall_drop: float | None


def drift_scores(evidently_metrics: list[dict]) -> dict[str, float]:
    """Column -> drift distance, from Evidently's `ValueDrift` results."""
    return {
        metric["config"]["column"]: float(metric["value"])
        for metric in evidently_metrics
        if metric.get("config", {}).get("type", "").endswith(":ValueDrift")
    }


def missing_shares(evidently_metrics: list[dict]) -> dict[str, float]:
    """Column -> share of missing values in the current data, from `MissingValueCount`."""
    shares = {}
    for metric in evidently_metrics:
        config = metric.get("config", {})
        if config.get("type", "").endswith(":MissingValueCount") and config.get("column"):
            value = metric["value"]
            shares[config["column"]] = float(value["share"] if isinstance(value, dict) else value)
    return shares


def summarize_drift(scores: dict[str, float], config: DriftConfig) -> DriftSummary:
    """Distance-based methods: a column drifted when its distance reaches the threshold."""
    feature_scores = {c: scores[c] for c in FEATURE_COLUMNS if c in scores}
    drifted = [c for c, score in feature_scores.items() if score >= config.feature_threshold]
    share = len(drifted) / len(FEATURE_COLUMNS)
    prediction_score = scores.get(PREDICTION_COLUMN, 0.0)
    return DriftSummary(
        feature_scores={c: round(s, 4) for c, s in feature_scores.items()},
        drifted_features=drifted,
        drift_share=round(share, 4),
        dataset_drift=share >= config.dataset_drift_share,
        prediction_drift_score=round(prediction_score, 4),
        prediction_drift=prediction_score >= config.prediction_threshold,
    )


def summarize_quality(
    current: pd.DataFrame, current_missing: dict[str, float], reference: pd.DataFrame, config: DataQualityConfig
) -> DataQualitySummary:
    issues = []
    for column, share in current_missing.items():
        # Structural gaps (no resolution time without tickets) are not defects; their rate
        # follows the ticket-count distribution, which drift analysis already covers.
        if column in NULLABLE_FEATURES:
            continue
        reference_share = float(reference[column].isna().mean()) if column in reference else 0.0
        if share - reference_share > config.max_missing_share_increase:
            issues.append(f"{column}: missing share {share:.2f} vs reference {reference_share:.2f}")
    duplicates = float(current.duplicated(["customer_id", "snapshot_date"]).mean()) if len(current) else 0.0
    if duplicates > config.max_duplicate_share:
        issues.append(f"{duplicates:.1%} duplicate customer snapshots")
    return DataQualitySummary(
        rows=len(current),
        duplicate_share=round(duplicates, 4),
        missing_share={c: round(s, 4) for c, s in current_missing.items() if s > 0},
        issues=issues,
    )


def summarize_performance(labeled: pd.DataFrame, reference: dict, min_labeled: int) -> PerformanceSummary:
    """Production metrics of the predictions actually served, vs. the model's test metrics."""
    if len(labeled) < min_labeled:
        return PerformanceSummary(len(labeled), None, reference, None, None)
    metrics = metrics_for_predictions(labeled["actual_churn"], labeled["churn_prediction"], labeled["churn_probability"])
    selected = {k: metrics[k] for k in ("f1", "recall", "precision", "roc_auc", "churn_rate", "positive_rate")}

    def drop(name: str) -> float | None:
        if reference.get(name) is None or selected.get(name) is None:
            return None
        return round(reference[name] - selected[name], 4)

    return PerformanceSummary(len(labeled), selected, reference, drop("f1"), drop("recall"))


def run_evidently(reference: pd.DataFrame, current: pd.DataFrame, config: DriftConfig):
    """Run the Evidently report; returns (snapshot, metric list)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from evidently import DataDefinition, Dataset, Report
        from evidently.metrics import ValueDrift
        from evidently.presets import DataDriftPreset, DataSummaryPreset

    columns = [*FEATURE_COLUMNS, PREDICTION_COLUMN]
    definition = DataDefinition(
        numerical_columns=[*NUMERIC_FEATURES, PREDICTION_COLUMN],
        categorical_columns=[*CATEGORICAL_FEATURES, *BOOLEAN_FEATURES],
    )
    report = Report(
        [
            DataDriftPreset(
                columns=list(FEATURE_COLUMNS),
                drift_share=config.dataset_drift_share,
                num_method=config.numerical_method,
                cat_method=config.categorical_method,
                num_threshold=config.feature_threshold,
                cat_threshold=config.feature_threshold,
            ),
            ValueDrift(column=PREDICTION_COLUMN, method=config.numerical_method, threshold=config.prediction_threshold),
            DataSummaryPreset(columns=list(FEATURE_COLUMNS)),
        ]
    )
    snapshot = report.run(
        current_data=Dataset.from_pandas(current[columns].reset_index(drop=True), data_definition=definition),
        reference_data=Dataset.from_pandas(reference[columns].reset_index(drop=True), data_definition=definition),
    )
    return snapshot, snapshot.dict()["metrics"]
