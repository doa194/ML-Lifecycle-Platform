"""Candidate registration: turn the latest training-cycle winner into a registry version.

Only models produced by a complete, traceable training run may be registered: the run
must have finished, carry every lineage tag and have been evaluated on the test period.
Registration is idempotent - registering the same run twice returns the existing version -
so a retried retraining workflow never creates duplicate versions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from churn_platform.lifecycle.audit import record_event
from churn_platform.lifecycle.registry import ModelRegistry, utc_now
from churn_platform.lifecycle.states import (
    Alias,
    LifecycleError,
    Status,
    VersionState,
    check_transition,
)
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.tracking.lineage import REQUIRED_LINEAGE_TAGS

log = logging.getLogger(__name__)

REQUIRED_METRICS = ("val_f1", "test_f1", "test_recall", "test_roc_auc", "latency_p95_ms")
# Lineage tags copied from the run to the model version, so the registry alone can answer
# "which data and code produced this version?".
COPIED_TAGS = (*REQUIRED_LINEAGE_TAGS, "code.git_dirty", "model.uri")


def registration_problems(run_status: str, tags: dict[str, str], metrics: dict[str, float]) -> list[str]:
    """Why a run may not be registered (empty list = it may)."""
    problems = []
    if run_status != "FINISHED":
        problems.append(f"run status is {run_status}, expected FINISHED")
    missing_tags = [tag for tag in REQUIRED_LINEAGE_TAGS if not tags.get(tag)]
    if missing_tags:
        problems.append(f"missing lineage tags: {missing_tags}")
    if tags.get("evaluation.completed") != "true":
        problems.append("run was not evaluated on the test period (evaluation.completed tag missing)")
    missing_metrics = [metric for metric in REQUIRED_METRICS if metric not in metrics]
    if missing_metrics:
        problems.append(f"missing metrics: {missing_metrics}")
    return problems


@dataclass(frozen=True)
class RegistrationResult:
    version: VersionState
    created: bool


def register_from_workspace(registry: ModelRegistry, paths: PipelinePaths, ops_url: str) -> RegistrationResult:
    """Register the winner recorded in reports/ by the last `train` + `evaluate` stages."""
    summary = json.loads(paths.training_summary.read_text(encoding="utf-8"))
    evaluation = json.loads(paths.evaluation_report.read_text(encoding="utf-8"))
    winner = summary["winner"]
    if evaluation.get("run_id") != winner["run_id"]:
        raise LifecycleError("evaluation report does not belong to the training winner; run `dvc repro`")
    return register_candidate(registry, winner["run_id"], winner["model_uri"], ops_url)


def register_candidate(registry: ModelRegistry, run_id: str, model_uri: str, ops_url: str) -> RegistrationResult:
    existing = registry.find_version_by_run(run_id)
    if existing is not None:
        log.info("run %s is already registered as version %s", run_id, existing.version)
        return RegistrationResult(existing, created=False)

    run = registry.client.get_run(run_id)
    problems = registration_problems(run.info.status, run.data.tags, run.data.metrics)
    if problems:
        raise LifecycleError("run cannot be registered:\n  - " + "\n  - ".join(problems))
    if run.data.tags.get("model.uri") != model_uri:
        raise LifecycleError(f"model URI {model_uri} does not match the run's logged model {run.data.tags.get('model.uri')}")

    check_transition(None, Status.CANDIDATE)
    registry.ensure_registered_model()
    tags = {tag: run.data.tags[tag] for tag in COPIED_TAGS if tag in run.data.tags}
    tags.update({"lifecycle.status": Status.CANDIDATE, "lifecycle.registered_at": utc_now()})
    version = registry.create_version(model_uri, run_id, tags)
    # "candidate" always points at the newest cycle winner.
    registry.set_alias(Alias.CANDIDATE, version)
    record_event(
        ops_url,
        registry.model_name,
        version,
        "registered_candidate",
        {"run_id": run_id, "algorithm": tags.get("training.algorithm"), "data_id": tags.get("data.id"),
         "test_f1": run.data.metrics.get("test_f1")},
    )
    log.info("registered %s version %s as candidate", registry.model_name, version)
    return RegistrationResult(registry.version_state(version), created=True)
