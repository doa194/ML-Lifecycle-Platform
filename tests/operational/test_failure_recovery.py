"""Failure and recovery behaviour of the running platform.

Each test breaks something real (restarts PostgreSQL, stops MLflow, corrupts a model file in
MinIO) and checks the documented behaviour: the loaded champion keeps serving, a service
that cannot verify a model refuses to become ready, and everything recovers on its own once
the dependency is back. Every test restores the stack before it finishes.
"""

from __future__ import annotations

import time
from datetime import date

import boto3
import httpx
import pytest
from botocore.config import Config

from churn_platform.config import load_quality_gate_policy
from churn_platform.lifecycle.gate_runner import run_quality_gate
from churn_platform.lifecycle.promotion import promote
from churn_platform.lifecycle.registration import register_from_workspace
from churn_platform.lifecycle.states import Alias, LifecycleError
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.settings import read_dotenv
from tests.support.data import REPO_ROOT
from tests.support.lifecycle import LifecycleSandbox
from tests.support.processes import InferenceServer
from tests.support.serving import valid_payload
from tests.support.stack import compose, service_states, wait_until_healthy

pytestmark = pytest.mark.operational


def wait_for(check, timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(interval)
    return check()


def status(url: str, path: str) -> int:
    """HTTP status, or 0 while the service is not reachable (for example restarting)."""
    try:
        return httpx.get(f"{url}{path}", timeout=5).status_code
    except httpx.HTTPError:
        return 0


def ready_status(url: str) -> int:
    return status(url, "/ready")


def predict(url: str) -> int:
    payload = valid_payload()
    payload["snapshot_date"] = date(2027, 2, 1).isoformat()
    try:
        return httpx.post(f"{url}/predict", json=payload, timeout=10).status_code
    except httpx.HTTPError:
        return 0


def test_database_restart_does_not_break_serving(live_inference):
    compose("restart", "postgres")
    wait_until_healthy("postgres")

    assert wait_for(lambda: ready_status(live_inference) == 200, timeout=30)
    assert predict(live_inference) == 200


def test_loaded_champion_keeps_serving_while_mlflow_is_down(live_inference):
    compose("stop", "mlflow")
    try:
        during_outage = (ready_status(live_inference), predict(live_inference))
    finally:
        compose("start", "mlflow")
        wait_until_healthy("mlflow")

    # Serving never calls the registry after startup, so a registry outage is invisible.
    assert during_outage == (200, 200)


def test_service_started_during_mlflow_outage_waits_until_it_can_verify_a_model(live_inference):
    compose("stop", "mlflow")
    try:
        compose("restart", "inference")
        alive = wait_for(lambda: status(live_inference, "/health") == 200, timeout=60)
        outage = (ready_status(live_inference), predict(live_inference))
        metrics = httpx.get(f"{live_inference}/metrics", timeout=5).text
    finally:
        compose("start", "mlflow")
        wait_until_healthy("mlflow")

    # The retry loop (serving.yaml: 15 s) loads the champion once the registry is back.
    recovered = wait_for(lambda: ready_status(live_inference) == 200, timeout=90, interval=3)

    assert alive and outage == (503, 503)
    assert 'churn_model_load_failures_total{reason="registry_unavailable"}' in metrics
    assert recovered and predict(live_inference) == 200
    assert service_states()["monitoring-worker"]["State"] == "running"


@pytest.fixture(scope="module")
def tampered_sandbox(tmp_path_factory, stack):
    """A sandbox champion (v1) plus a registered candidate (v2) whose model file is then corrupted."""
    box = LifecycleSandbox(tmp_path_factory.mktemp("tamper")).create()
    box.repro()
    settings, registry = box.settings(), box.registry()
    first = register_from_workspace(registry, PipelinePaths(box.root), settings.ops_database_url).version.version
    run_quality_gate(registry, box.root, settings.config_dir, settings.ops_database_url, first)
    promote(registry, load_quality_gate_policy(settings.config_dir), settings.ops_database_url)
    box.retrain()
    second = register_from_workspace(registry, PipelinePaths(box.root), settings.ops_database_url).version.version
    box.first, box.second = first, second
    yield box
    box.cleanup()


def model_objects(model_id: str):
    env = read_dotenv(REPO_ROOT / ".env")
    s3 = boto3.client("s3", endpoint_url="http://127.0.0.1:9000", aws_access_key_id=env["MLFLOW_S3_ACCESS_KEY"],
                      aws_secret_access_key=env["MLFLOW_S3_SECRET_KEY"], region_name="us-east-1",
                      config=Config(signature_version="s3v4"))
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket="mlflow-artifacts")
    keys = [o["Key"] for page in pages for o in page.get("Contents", []) if f"/{model_id}/" in o["Key"]]
    return s3, keys


def test_corrupted_model_artifact_is_refused_everywhere(tampered_sandbox):
    box = tampered_sandbox
    settings, registry = box.settings(), box.registry()
    model_id = registry.client.get_model_version(box.model_name, box.second).source.split("/")[-1]
    s3, keys = model_objects(model_id)
    skops_key = next(k for k in keys if k.endswith("model.skops"))
    s3.put_object(Bucket="mlflow-artifacts", Key=skops_key, Body=b"tampered weights")

    decision = run_quality_gate(registry, box.root, settings.config_dir, settings.ops_database_url, box.second)
    registry.set_alias(Alias.CHAMPION, box.second)  # simulate an operator bypassing the lifecycle tooling
    server = InferenceServer(box)
    try:
        ready = httpx.get(f"{server.launch().url}/ready", timeout=5).json()
    finally:
        server.stop()
        registry.set_alias(Alias.CHAMPION, box.first)

    assert [c.name for c in decision.failed_checks] == ["loadable_and_intact"]
    assert "ModelIntegrityError" in ready["reason"]
    with pytest.raises(LifecycleError):
        promote(registry, load_quality_gate_policy(settings.config_dir), settings.ops_database_url, box.second)


def test_missing_model_artifact_leaves_the_service_not_ready(tampered_sandbox):
    box = tampered_sandbox
    registry = box.registry()
    model_id = registry.client.get_model_version(box.model_name, box.second).source.split("/")[-1]
    s3, keys = model_objects(model_id)
    for key in keys:
        s3.delete_object(Bucket="mlflow-artifacts", Key=key)

    registry.set_alias(Alias.CHAMPION, box.second)
    server = InferenceServer(box)
    try:
        ready = httpx.get(f"{server.launch().url}/ready", timeout=5)
    finally:
        server.stop()
        registry.set_alias(Alias.CHAMPION, box.first)

    assert ready.status_code == 503 and ready.json()["status"] == "not_ready"
