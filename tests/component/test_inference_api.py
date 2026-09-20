"""Inference API contract, exercised over HTTP with an in-process fake registry and store.

Protects the externally visible behaviour: the prediction contract, rejection of invalid
input before it reaches the model, liveness vs. readiness, model lineage, failure
semantics (no model, failed persistence) and the metrics endpoint. Model-quality and
gate rules are covered by unit tests and are deliberately not repeated here.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from churn_platform.serving.app import create_app
from churn_platform.settings import load_settings
from churn_platform.tracking.model_io import ModelIntegrityError
from tests.support.serving import (
    FakeStore,
    serving_config,
    serving_model,
    valid_payload,
)

pytestmark = pytest.mark.component


class CountingLoader:
    """Returns the test model; counts predictions so tests can prove the model was not called."""

    def __init__(self, failures: list[Exception] | None = None):
        self.failures = list(failures or [])
        self.model = serving_model()
        self.predict_calls = 0
        original = self.model.model.predict_proba

        def counting_predict(frame):
            self.predict_calls += 1
            return original(frame)

        self.model.model.predict_proba = counting_predict

    def __call__(self):
        if self.failures:
            raise self.failures.pop(0)
        return self.model


def client_for(loader=None, store=None, **config_changes) -> TestClient:
    app = create_app(
        settings=load_settings(),
        serving_config=serving_config(**config_changes),
        loader=loader or CountingLoader(),
        store=store or FakeStore(),
    )
    return TestClient(app)


def test_valid_request_returns_prediction_from_the_served_version_and_records_it():
    store = FakeStore()
    with client_for(store=store) as client:
        response = client.post("/predict", json=valid_payload())

    body = response.json()
    assert response.status_code == 200
    assert set(body) == {
        "prediction_id", "customer_id", "snapshot_date", "churn_prediction", "churn_probability",
        "risk_level", "decision_threshold", "model_name", "model_version",
    }
    assert body["model_version"] == "7" and 0.0 <= body["churn_probability"] <= 1.0
    assert body["churn_prediction"] == (body["risk_level"] == "high")
    [observation] = store.records
    assert str(observation.prediction_id) == body["prediction_id"]
    assert observation.model_version == "7" and observation.features["tenure_months"] == 3


@pytest.mark.parametrize(
    "payload",
    [
        valid_payload(tenure_months=-1),
        valid_payload(plan_tier="platinum"),
        valid_payload(autopay_enabled="yes"),
        valid_payload(sessions_30d=4.5),
        valid_payload(complaints_90d=5),
        valid_payload(support_tickets_90d=0, complaints_90d=0),  # resolution time without tickets
        valid_payload(cancellation_status="requested"),  # outcome leakage field
        {"customer_id": "C-1", "features": {"plan_tier": "basic"}},
        {**valid_payload(), "customer_id": "bad id with spaces"},
    ],
)
def test_invalid_requests_are_rejected_before_reaching_the_model(payload):
    loader, store = CountingLoader(), FakeStore()
    with client_for(loader=loader, store=store) as client:
        response = client.post("/predict", json=payload)

    assert response.status_code == 422
    assert loader.predict_calls == 0 and store.records == []


def test_health_ready_and_model_lineage_when_champion_is_loaded():
    with client_for() as client:
        health, ready, model = client.get("/health"), client.get("/ready"), client.get("/model")

    assert health.json() == {"status": "alive", "model_loaded": True}
    assert ready.status_code == 200 and ready.json()["model_version"] == "7"
    lineage = model.json()
    assert lineage["alias"] == "champion" and lineage["run_id"] == "run-123"
    assert lineage["dataset"]["id"] == "customer-snapshots@2026-01-01#abc"
    assert lineage["decision_threshold"] == 0.55 and lineage["test_metrics"]["f1"] == 0.5


@pytest.mark.parametrize(
    ("error", "reason"),
    [(ModelIntegrityError("fingerprint mismatch"), "integrity"), (RuntimeError("RESOURCE_DOES_NOT_EXIST: alias"), "no_champion")],
)
def test_model_load_failure_keeps_the_service_alive_but_not_ready(error, reason):
    with client_for(loader=CountingLoader(failures=[error]), retry=0) as client:
        health, ready = client.get("/health"), client.get("/ready")
        predict, model = client.post("/predict", json=valid_payload()), client.get("/model")
        metrics = client.get("/metrics").text

    assert health.status_code == 200 and health.json()["model_loaded"] is False
    assert ready.status_code == 503 and ready.json()["status"] == "not_ready"
    assert predict.status_code == 503 and model.status_code == 503
    assert f'churn_model_load_failures_total{{reason="{reason}"}} 1.0' in metrics
    assert "churn_model_loaded 0.0" in metrics


def test_service_becomes_ready_once_the_registry_is_reachable_again():
    loader = CountingLoader(failures=[ConnectionError("Max retries exceeded")])
    with client_for(loader=loader, retry=0.05) as client:
        assert client.get("/ready").status_code == 503
        deadline = time.monotonic() + 5
        while client.get("/ready").status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.05)
        ready = client.get("/ready")

    assert ready.status_code == 200


def test_unrecorded_prediction_is_refused_in_reject_mode():
    with client_for(store=FakeStore(fail=True), on_persistence_failure="reject") as client:
        response = client.post("/predict", json=valid_payload())
        metrics = client.get("/metrics").text

    assert response.status_code == 503
    assert "churn_observation_write_failures_total 1.0" in metrics


def test_unrecorded_prediction_is_served_in_serve_mode():
    with client_for(store=FakeStore(fail=True), on_persistence_failure="serve") as client:
        response = client.post("/predict", json=valid_payload())

    assert response.status_code == 200


def test_readiness_requires_the_observation_store_in_reject_mode():
    with client_for(store=FakeStore(healthy=False), on_persistence_failure="reject") as client:
        response = client.get("/ready")

    assert response.status_code == 503 and response.json()["reason"] == "observation store unavailable"


def test_metrics_expose_requests_predictions_and_model_identity():
    with client_for() as client:
        client.post("/predict", json=valid_payload())
        client.post("/predict", json=valid_payload(tenure_months=-5))
        metrics = client.get("/metrics").text

    assert 'churn_http_requests_total{method="POST",route="/predict",status="200"} 1.0' in metrics
    assert 'churn_http_requests_total{method="POST",route="/predict",status="422"} 1.0' in metrics
    assert 'model_version="7"' in metrics and "churn_predictions_total" in metrics
    assert "churn_model_loaded 1.0" in metrics
    assert "C-42" not in metrics  # no customer identifiers as metric labels


def test_oversized_request_body_is_rejected():
    with client_for() as client:
        response = client.post("/predict", content=b"{" + b" " * 20000 + b"}", headers={"content-type": "application/json"})

    assert response.status_code == 413
