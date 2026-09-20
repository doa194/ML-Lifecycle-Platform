"""Training cycle + registration against the real MLflow server, MinIO and PostgreSQL.

Protects traceability: every run of a cycle carries lineage that matches DVC's hashes, the
winner is evaluated and ships its monitoring reference, the stored model loads in a fresh
process only when its fingerprint matches, and registration is idempotent.
"""

from __future__ import annotations

import json
import subprocess
import sys

import mlflow
import pandas as pd
import pytest
import yaml

from churn_platform.lifecycle.registration import register_from_workspace
from churn_platform.lifecycle.states import Alias, Status
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.tracking.lineage import REQUIRED_LINEAGE_TAGS
from churn_platform.tracking.model_io import ModelIntegrityError, load_model
from tests.support.lifecycle import LifecycleSandbox

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory, stack):
    box = LifecycleSandbox(tmp_path_factory.mktemp("training")).create()
    box.repro()
    yield box
    box.cleanup()


@pytest.fixture(scope="module")
def summary(sandbox):
    return json.loads(PipelinePaths(sandbox.root).training_summary.read_text(encoding="utf-8"))


def test_cycle_logs_one_run_per_algorithm_with_complete_lineage(sandbox, summary):
    client = sandbox.registry().client
    runs = [client.get_run(candidate["run_id"]) for candidate in summary["candidates"]]
    lock = yaml.safe_load((sandbox.root / "dvc.lock").read_text(encoding="utf-8"))
    dvc_train_md5 = next(o["md5"] for o in lock["stages"]["split"]["outs"] if o["path"] == "data/splits/train.parquet")

    assert sorted(r.data.tags["training.algorithm"] for r in runs) == sorted(
        ["logistic_regression", "random_forest", "hist_gradient_boosting"]
    )
    for run in runs:
        assert all(run.data.tags.get(tag) for tag in REQUIRED_LINEAGE_TAGS), run.data.tags
        assert run.data.tags["data.train_md5"] == dvc_train_md5
        assert run.data.tags["training.cycle_id"] == summary["cycle_id"]
        assert "val_f1" in run.data.metrics and "decision_threshold" in run.data.params


def test_cycle_run_keeps_the_configuration_and_dvc_lock_it_used(sandbox, summary):
    artifacts = {a.path for a in sandbox.registry().client.list_artifacts(summary["cycle_run_id"], "lineage")}

    assert {"lineage/params.yaml", "lineage/dvc.lock", "lineage/training.yaml", "lineage/data.yaml"} <= artifacts


def test_winner_is_evaluated_and_ships_its_monitoring_reference(sandbox, summary, tmp_path):
    run = sandbox.registry().client.get_run(summary["winner"]["run_id"])
    reference_path = mlflow.artifacts.download_artifacts(
        run_id=run.info.run_id, artifact_path="reference/reference.parquet", dst_path=str(tmp_path)
    )
    reference = pd.read_parquet(reference_path)

    assert run.data.tags["evaluation.completed"] == "true"
    assert {"test_f1", "test_recall", "test_roc_auc", "latency_p95_ms"} <= run.data.metrics.keys()
    assert {"churn_probability", "churn_prediction", "churned_30d"} <= set(reference.columns)
    assert len(reference) > 0


def test_stored_model_loads_in_a_fresh_process(sandbox, summary):
    winner = summary["winner"]
    script = (
        "from churn_platform.settings import load_settings\n"
        "from churn_platform.tracking.mlflow_setup import configure_mlflow\n"
        "from churn_platform.tracking.model_io import load_model\n"
        "configure_mlflow(load_settings())\n"
        f"model = load_model({winner['model_uri']!r}, {winner['fingerprint']!r})\n"
        "print(model.decision_threshold)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=sandbox.root, check=True)

    assert float(result.stdout.strip().splitlines()[-1]) == pytest.approx(winner["decision_threshold"])


def test_model_with_unexpected_fingerprint_is_refused(sandbox, summary):
    sandbox.settings()

    with pytest.raises(ModelIntegrityError):
        load_model(summary["winner"]["model_uri"], expected_fingerprint="0" * 64)


def test_registration_creates_one_loadable_candidate_version(sandbox, summary):
    settings = sandbox.settings()
    registry = sandbox.registry()

    first = register_from_workspace(registry, PipelinePaths(sandbox.root), settings.ops_database_url)
    second = register_from_workspace(registry, PipelinePaths(sandbox.root), settings.ops_database_url)
    model = load_model(registry.model_uri(first.version.version), first.version.tags["model.fingerprint"])

    assert first.created and not second.created
    assert second.version.version == first.version.version
    assert first.version.aliases == {Alias.CANDIDATE}
    assert first.version.status == Status.CANDIDATE
    assert registry.run_id(first.version.version) == summary["winner"]["run_id"]
    assert model.decision_threshold == pytest.approx(summary["winner"]["decision_threshold"])
