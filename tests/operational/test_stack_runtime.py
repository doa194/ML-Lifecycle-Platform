"""Operational checks against the real Docker Compose stack.

These verify runtime behavior that configuration files alone cannot prove: services become
healthy, published ports are reachable only from this machine, and tracking data survives
container restarts. Restart tests are disruptive, so this module is opt-in (`-m operational`).
"""

from __future__ import annotations

import mlflow
import pytest
from mlflow.tracking import MlflowClient

from tests.support.stack import restart_and_wait, service_states

pytestmark = pytest.mark.operational

LONG_RUNNING = ("postgres", "minio", "mlflow")


def test_long_running_services_are_healthy(stack):
    states = service_states()

    unhealthy = {name: states.get(name, {}).get("Health") for name in LONG_RUNNING}
    assert all(health == "healthy" for health in unhealthy.values()), unhealthy
    assert states["minio-init"]["State"] == "exited" and states["minio-init"]["ExitCode"] == 0


def test_published_ports_are_bound_to_loopback_only(stack):
    publishers = [
        (name, publisher)
        for name, state in service_states().items()
        for publisher in state.get("Publishers") or []
        if publisher.get("PublishedPort")
    ]

    assert publishers, "expected published ports"
    exposed = [(n, p["URL"], p["PublishedPort"]) for n, p in publishers if p["URL"] not in ("127.0.0.1",)]
    assert not exposed, f"ports reachable from other machines: {exposed}"


def test_tracking_metadata_and_artifacts_survive_restarts(stack, unique_name, tmp_path):
    artifact = tmp_path / "evidence.txt"
    artifact.write_text("persisted", encoding="utf-8")
    mlflow.set_experiment(unique_name)
    with mlflow.start_run() as run:
        mlflow.log_metric("f1", 0.7)
        mlflow.log_artifact(str(artifact))

    restart_and_wait(*LONG_RUNNING)

    stored = MlflowClient(stack.mlflow_tracking_uri).get_run(run.info.run_id)
    downloaded = mlflow.artifacts.download_artifacts(
        run_id=run.info.run_id, artifact_path="evidence.txt", dst_path=str(tmp_path / "after")
    )
    assert stored.data.metrics["f1"] == pytest.approx(0.7)
    assert open(downloaded, encoding="utf-8").read() == "persisted"
