"""Monitoring against real observations, Evidently, MLflow and PostgreSQL.

Protects: observations served by the real API become a monitoring record and an Evidently
report; a controlled drift profile produces the expected drift signal and exactly one
retraining request; delayed outcomes update only the predictions whose window closed, with
the correct values, and then enable performance monitoring.
"""

from __future__ import annotations

from datetime import date

import mlflow
import pytest
from fastapi.testclient import TestClient

from churn_platform.config import (
    load_data_config,
    load_monitoring_config,
    load_retraining_config,
    load_serving_config,
)
from churn_platform.monitoring.runner import SKIPPED, run_monitoring_cycle
from churn_platform.retraining.policy import Decision
from churn_platform.retraining.requests import list_requests
from churn_platform.serving.app import create_app
from churn_platform.simulation.outcomes import resolve_outcomes
from churn_platform.simulation.traffic import build_requests, store_hidden_outcomes
from churn_platform.storage.db import connect

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def world(champion_sandbox):
    settings = champion_sandbox.settings()
    app = create_app(settings=settings, serving_config=load_serving_config(settings.config_dir))
    with TestClient(app) as client:
        yield champion_sandbox, settings, client


def send(world, start, end, count, seed, profile=None):
    box, settings, client = world
    requests = build_requests(load_data_config(settings.config_dir), start, end, count, seed, profile)
    store_hidden_outcomes(settings.ops_database_url, requests)
    for request in requests:
        assert client.post("/predict", json=request.payload).status_code == 200
    return requests


def monitor(settings, **kwargs):
    return run_monitoring_cycle(
        settings, load_monitoring_config(settings.config_dir), load_retraining_config(settings.config_dir).policy, **kwargs
    )


def test_drift_is_detected_only_after_the_world_changes_and_requests_retraining_once(world):
    box, settings, _ = world

    send(world, date(2025, 11, 1), date(2025, 12, 31), 600, seed=910001)
    stable = monitor(settings)
    send(world, date(2026, 1, 1), date(2026, 12, 31), 900, seed=910002)
    drifted = monitor(settings)
    repeated = monitor(settings)
    unchanged = monitor(settings, skip_if_unchanged=True)

    assert not stable.drift.dataset_drift and not stable.drift.prediction_drift
    # A single noisy feature may be "watched", but stable data never requests retraining.
    assert stable.decision in {Decision.NO_ACTION, Decision.WATCH}
    assert drifted.drift.dataset_drift and drifted.drift.prediction_drift
    assert {"recent_price_increase", "payment_failures_90d"} <= set(drifted.drift.drifted_features)
    assert drifted.decision == Decision.REQUEST_RETRAINING
    assert repeated.decision == Decision.BLOCKED and repeated.retraining_request_id is None
    assert unchanged.decision == SKIPPED
    requests = list_requests(settings.ops_database_url, box.model_name)
    assert len(requests) == 1 and requests[0]["status"] == "pending"
    assert requests[0]["data_as_of"] <= date(2026, 12, 31)


def test_each_monitoring_run_is_recorded_with_its_evidently_report(world):
    box, settings, _ = world
    send(world, date(2025, 10, 1), date(2025, 10, 31), 250, seed=910003)

    result = monitor(settings)

    with connect(settings.ops_database_url) as conn:
        row = conn.execute(
            "SELECT model_version, reference_run_id, observation_count, report_run_id, input_fingerprint "
            "FROM ops.monitoring_runs WHERE monitoring_run_id = %s",
            (result.monitoring_run_id,),
        ).fetchone()
    artifacts = {a.path for a in mlflow.MlflowClient().list_artifacts(row[3], "monitoring")}
    assert row[0] == box.champion_version
    assert row[1] == box.registry().run_id(box.champion_version)
    assert row[2] == result.observation_count and row[4]
    assert {"monitoring/evidently_report.html", "monitoring/summary.json"} <= artifacts


def test_delayed_outcomes_resolve_only_closed_windows_and_enable_performance_monitoring(world):
    box, settings, _ = world
    requests = send(world, date(2025, 9, 1), date(2025, 9, 30), 250, seed=910004)
    truth = {r.payload["customer_id"]: r.churned for r in requests}

    early = resolve_outcomes(settings.ops_database_url, date(2025, 9, 20), 30, box.model_name)
    later = resolve_outcomes(settings.ops_database_url, date(2025, 10, 31), 30, box.model_name)
    with connect(settings.ops_database_url) as conn:
        rows = conn.execute(
            "SELECT customer_id, actual_churn, outcome_resolved_at FROM ops.prediction_observations "
            "WHERE model_name = %s AND customer_id = ANY(%s)",
            (box.model_name, list(truth)),
        ).fetchall()
    result = monitor(settings)

    assert early.resolved == 0  # no September snapshot is 30 days old on 2025-09-20
    assert later.resolved >= len(requests)
    assert all(actual == truth[customer] and resolved_at is not None for customer, actual, resolved_at in rows)
    assert result.performance.labeled_count >= len(requests)
    assert result.performance.metrics["roc_auc"] > 0.7
