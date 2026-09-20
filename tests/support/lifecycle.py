"""A disposable lifecycle sandbox for integration and E2E tests.

It combines an isolated workspace (own Git repo, DVC cache and MinIO prefix) with a unique
MLflow experiment and registered-model name, so tests exercise the real pipeline, tracking
server and registry without touching `customer-churn-classifier` or the demo data.
Everything is removed again on `cleanup()`.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import yaml
from mlflow.tracking import MlflowClient

from churn_platform.lifecycle.registry import ModelRegistry
from churn_platform.settings import Settings, load_settings
from churn_platform.storage.db import connect
from churn_platform.tracking.mlflow_setup import configure_mlflow
from tests.support.workspace import IsolatedWorkspace, dvc

# A deliberately useless model (one depth-1 tree) for "weak candidate" scenarios.
WEAK_ALGORITHMS = {
    "random_forest": {"n_estimators": 1, "max_depth": 1, "min_samples_leaf": 10, "class_weight": "balanced_subsample", "n_jobs": 1}
}


class LifecycleSandbox:
    def __init__(self, root: Path):
        suffix = uuid.uuid4().hex[:8]
        self.workspace = IsolatedWorkspace(root / "ws")
        self.model_name = f"test-churn-{suffix}"
        self.experiment_name = f"test-churn-{suffix}"

    def create(self) -> LifecycleSandbox:
        self.workspace.create()
        return self

    @property
    def root(self) -> Path:
        return self.workspace.root

    @property
    def env(self) -> dict[str, str]:
        return {"CHURN_MODEL_NAME": self.model_name, "CHURN_EXPERIMENT_NAME": self.experiment_name}

    def settings(self) -> Settings:
        settings = dataclasses.replace(
            load_settings(workspace=self.root), model_name=self.model_name, experiment_name=self.experiment_name
        )
        configure_mlflow(settings)
        return settings

    def registry(self) -> ModelRegistry:
        settings = self.settings()
        return ModelRegistry(MlflowClient(settings.mlflow_tracking_uri), settings.model_name)

    def repro(self, *args: str) -> None:
        dvc(self.root, "repro", *args, extra_env=self.env)

    def retrain(self) -> None:
        """Force a new training cycle on the current data (new MLflow runs, same inputs)."""
        self.repro("--force", "--single-item", "train")
        self.repro("evaluate")

    def set_algorithms(self, algorithms: dict | None) -> None:
        """Replace the algorithm section of training.yaml (None restores the repository default)."""
        path = self.root / "config" / "training.yaml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        if algorithms is None:
            original = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "training.yaml").read_text(encoding="utf-8"))
            algorithms = original["algorithms"]
        config["algorithms"] = algorithms
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def cleanup(self) -> None:
        settings = self.settings()
        client = MlflowClient(settings.mlflow_tracking_uri)
        try:
            client.delete_registered_model(self.model_name)
        except Exception:  # noqa: BLE001 - nothing registered is fine
            pass
        for name in (self.experiment_name, f"{self.experiment_name}-monitoring"):
            experiment = client.get_experiment_by_name(name)
            if experiment is not None:
                client.delete_experiment(experiment.experiment_id)
        with connect(settings.ops_database_url) as conn:
            conn.execute(
                "DELETE FROM simulation.customer_outcomes WHERE customer_id IN "
                "(SELECT customer_id FROM ops.prediction_observations WHERE model_name = %s)",
                (self.model_name,),
            )
            for table in ("lifecycle_events", "prediction_observations", "monitoring_runs", "retraining_requests"):
                conn.execute(f"DELETE FROM ops.{table} WHERE model_name = %s", (self.model_name,))
        self.workspace.delete_remote()
