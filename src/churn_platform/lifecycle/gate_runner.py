"""Runs the quality gate against the real registry and records the outcome.

Steps:
 1. pick the version (default: the `candidate` alias)
 2. confirm the local test split is exactly the data the candidate was built from (hash
    check), so the gate never evaluates on the wrong dataset
 3. load the candidate and the current champion through the verified loader and evaluate
    both on that same test split
 4. apply the pure gate rules (`quality_gate.evaluate_gate`)
 5. record the report on the candidate's MLflow run, tag the version and move aliases:
    passed -> `challenger` alias; failed -> status `rejected`
The champion alias is never touched here.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from churn_platform.config import load_quality_gate_policy, load_training_config
from churn_platform.lifecycle.audit import record_event
from churn_platform.lifecycle.quality_gate import (
    GateDecision,
    ModelEvaluation,
    evaluate_gate,
)
from churn_platform.lifecycle.registry import ModelRegistry, utc_now
from churn_platform.lifecycle.states import (
    NO_CHAMPION,
    Alias,
    LifecycleError,
    Status,
    VersionState,
    check_transition,
)
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.pipeline.stages import file_md5
from churn_platform.tracking.model_io import load_model
from churn_platform.training.evaluation import evaluate_model

log = logging.getLogger(__name__)


def evaluate_version(registry: ModelRegistry, state: VersionState, test: pd.DataFrame, config_dir: Path) -> ModelEvaluation:
    fingerprint = state.tags.get("model.fingerprint")
    if not fingerprint:
        return ModelEvaluation(state.version, load_error="version has no recorded model fingerprint")
    try:
        model = load_model(registry.model_uri(state.version), expected_fingerprint=fingerprint)
    except Exception as error:  # noqa: BLE001 - any load failure is a gate failure, not a crash
        return ModelEvaluation(state.version, load_error=f"{type(error).__name__}: {error}")
    result = evaluate_model(model, test, load_training_config(config_dir))
    return ModelEvaluation(
        version=state.version,
        overall=result["overall"],
        segments=result["segments"],
        latency_p95_ms=result["latency"]["p95_ms"],
    )


def run_quality_gate(registry: ModelRegistry, workspace: Path, config_dir: Path, ops_url: str, version: str | None = None) -> GateDecision:
    candidate = registry.version_state(version) if version else registry.alias_state(Alias.CANDIDATE)
    if candidate is None:
        raise LifecycleError("no version holds the 'candidate' alias; register a training winner first")
    check_transition(candidate.status, Status.CHALLENGER)

    test_path = PipelinePaths(workspace).split("test")
    expected_md5 = candidate.tags.get("data.test_md5")
    if not test_path.exists() or file_md5(test_path) != expected_md5:
        raise LifecycleError(
            f"local test split does not match version {candidate.version}'s lineage (data.test_md5={expected_md5}); "
            "check out that dataset version (git checkout + dvc checkout/pull) before running the gate"
        )
    test = pd.read_parquet(test_path)

    champion = registry.alias_state(Alias.CHAMPION)
    candidate_eval = evaluate_version(registry, candidate, test, config_dir)
    champion_eval = None
    if champion is not None:
        champion_eval = evaluate_version(registry, champion, test, config_dir)
        if champion_eval.load_error:
            # Without a trustworthy comparison the safe answer is "no decision": nothing changes.
            raise LifecycleError(f"current champion v{champion.version} cannot be evaluated: {champion_eval.load_error}")

    decision = evaluate_gate(candidate_eval, champion_eval, load_quality_gate_policy(config_dir))
    _record(registry, candidate, champion, decision, candidate_eval, champion_eval, ops_url)
    return decision


def _record(registry, candidate, champion, decision, candidate_eval, champion_eval, ops_url) -> None:
    evaluated_at = utc_now()
    report = {
        **decision.to_dict(),
        "evaluated_at": evaluated_at,
        "evaluation_data_md5": candidate.tags.get("data.test_md5"),
        "candidate_metrics": candidate_eval.overall,
        "champion_metrics": champion_eval.overall if champion_eval else None,
    }
    artifact = f"gate/report-v{candidate.version}-{evaluated_at.replace(':', '')}.json"
    registry.client.log_dict(registry.run_id(candidate.version), report, artifact)

    tags = {
        "gate.status": "passed" if decision.passed else "failed",
        "gate.evaluated_at": evaluated_at,
        "gate.champion_version": champion.version if champion else NO_CHAMPION,
        "gate.report_artifact": artifact,
        "gate.failed_checks": ",".join(check.name for check in decision.failed_checks) or "none",
    }
    if decision.passed:
        previous = registry.alias_state(Alias.CHALLENGER)
        registry.set_alias(Alias.CHALLENGER, candidate.version)
        registry.set_tags(candidate.version, {**tags, "lifecycle.status": Status.CHALLENGER})
        if previous is not None and previous.version != candidate.version:
            # Only one challenger at a time; the older one lost its eligibility.
            registry.set_tags(previous.version, {"lifecycle.status": Status.REJECTED,
                                                 "lifecycle.rejected_reason": f"superseded by challenger v{candidate.version}"})
    else:
        if Alias.CHALLENGER in candidate.aliases:
            registry.delete_alias(Alias.CHALLENGER)
        registry.set_tags(candidate.version, {**tags, "lifecycle.status": Status.REJECTED,
                                              "lifecycle.rejected_reason": "failed quality gate"})
    record_event(
        ops_url,
        registry.model_name,
        candidate.version,
        "gate_passed" if decision.passed else "gate_failed",
        {"champion_version": tags["gate.champion_version"], "failed_checks": tags["gate.failed_checks"], "report": artifact},
    )
    log.info("quality gate for v%s: %s", candidate.version, "PASSED" if decision.passed else f"FAILED ({tags['gate.failed_checks']})")
