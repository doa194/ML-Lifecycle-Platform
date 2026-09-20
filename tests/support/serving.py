"""Fakes and fixtures for testing the inference API without MLflow or PostgreSQL."""

from __future__ import annotations

from functools import lru_cache

from churn_platform.config import load_serving_config
from churn_platform.data.schema import FEATURE_COLUMNS, LABEL_COLUMN
from churn_platform.features.preprocessing import build_model_pipeline
from churn_platform.serving.model_state import ServingModel
from churn_platform.tracking.model_io import LoadedModel
from churn_platform.training.algorithms import build_classifier
from tests.support.data import REPO_CONFIG, small_dataset


@lru_cache(maxsize=1)
def trained_model() -> LoadedModel:
    data = small_dataset(rows=3000, seed=17)
    pipeline = build_model_pipeline(build_classifier("logistic_regression", {"max_iter": 500, "class_weight": "balanced"}, 0), 6)
    pipeline.fit(data.loc[:, list(FEATURE_COLUMNS)], data[LABEL_COLUMN])
    return LoadedModel(pipeline=pipeline, decision_threshold=0.55, fingerprint="f" * 64, model_uri="models:/test/7")


def serving_model() -> ServingModel:
    return ServingModel(
        model=trained_model(),
        name="customer-churn-classifier",
        version="7",
        run_id="run-123",
        tags={"training.algorithm": "logistic_regression", "data.id": "customer-snapshots@2026-01-01#abc", "data.as_of": "2026-01-01",
              "gate.status": "passed", "code.git_commit": "abc123"},
        run_metrics={"test_f1": 0.5, "val_f1": 0.52},
    )


class FakeStore:
    def __init__(self, fail: bool = False, healthy: bool = True):
        self.records = []
        self.fail = fail
        self._healthy = healthy

    def open(self):
        pass

    def record(self, observation):
        if self.fail:
            raise ConnectionError("database down")
        self.records.append(observation)

    def healthy(self):
        return self._healthy

    def close(self):
        pass


def serving_config(**changes):
    config = load_serving_config(REPO_CONFIG)
    if "on_persistence_failure" in changes:
        config = config.model_copy(update={"observations": config.observations.model_copy(update={"on_persistence_failure": changes.pop("on_persistence_failure")})})
    if "retry" in changes:
        config = config.model_copy(update={"model_loading": config.model_loading.model_copy(update={"retry_interval_seconds": changes.pop("retry")})})
    return config


def valid_payload(**feature_overrides) -> dict:
    features = {
        "plan_tier": "basic",
        "contract_type": "monthly",
        "autopay_enabled": False,
        "recent_price_increase": True,
        "tenure_months": 3,
        "monthly_charge": 24.5,
        "payment_failures_90d": 2,
        "late_payments_12m": 3,
        "monthly_usage_hours": 4.0,
        "sessions_30d": 3,
        "days_since_last_login": 21,
        "usage_trend_pct": -0.4,
        "support_tickets_90d": 2,
        "complaints_90d": 1,
        "avg_resolution_hours": 30.0,
        "features_adopted": 1,
    }
    features.update(feature_overrides)
    return {"customer_id": "C-42", "snapshot_date": "2026-02-01", "features": features}
