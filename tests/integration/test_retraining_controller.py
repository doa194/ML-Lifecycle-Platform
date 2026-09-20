"""Retraining controller against the real DVC pipeline, MLflow registry and PostgreSQL.

Protects the continuous-training failure semantics: a request re-runs the pipeline on the
requested data, a rejected candidate or a failing pipeline never changes the champion, a
failed request can be retried safely, and duplicate requests or claims are impossible.
"""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from churn_platform.config import load_quality_gate_policy, load_retraining_config
from churn_platform.lifecycle.gate_runner import run_quality_gate
from churn_platform.lifecycle.promotion import promote
from churn_platform.lifecycle.registration import register_from_workspace
from churn_platform.lifecycle.states import Alias
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.retraining.controller import process_request
from churn_platform.retraining.requests import claim_next, create_request, list_requests
from churn_platform.storage.db import connect
from tests.support.lifecycle import WEAK_ALGORITHMS, LifecycleSandbox

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def box(tmp_path_factory, stack):
    sandbox = LifecycleSandbox(tmp_path_factory.mktemp("retraining")).create()
    sandbox.repro()
    settings = sandbox.settings()
    registry = sandbox.registry()
    version = register_from_workspace(registry, PipelinePaths(sandbox.root), settings.ops_database_url).version.version
    run_quality_gate(registry, sandbox.root, settings.config_dir, settings.ops_database_url, version)
    promote(registry, load_quality_gate_policy(settings.config_dir), settings.ops_database_url)
    sandbox.champion_version = version
    yield sandbox
    sandbox.cleanup()


def new_request(box, as_of: date, reason: str):
    settings = box.settings()
    with connect(settings.ops_database_url) as conn:
        request_id = create_request(conn, box.model_name, "manual", [reason], as_of)
    assert request_id is not None
    return claim_next(settings.ops_database_url, box.model_name, 60, request_id)


def controller_config(box):
    config = load_retraining_config(box.settings().config_dir)
    # The test workspace's data is not pushed; DVC push to MinIO is covered elsewhere.
    return config.model_copy(update={"controller": config.controller.model_copy(update={"push_data": False})})


def test_rejected_candidate_is_trained_on_the_requested_data_and_leaves_the_champion(box):
    box.set_algorithms(WEAK_ALGORITHMS)
    try:
        request = new_request(box, date(2026, 7, 1), "weak model scenario")
        outcome = process_request(box.settings(), box.registry(), request, controller_config(box))
    finally:
        box.set_algorithms(None)
    registry = box.registry()
    candidate = registry.version_state(outcome.candidate_version)
    params = yaml.safe_load((box.root / "params.yaml").read_text(encoding="utf-8"))
    [row] = [r for r in list_requests(box.settings().ops_database_url, box.model_name) if r["request_id"] == request.request_id]

    assert outcome.outcome == "rejected"
    assert params["dataset"]["as_of"] == "2026-07-01"
    assert candidate.tags["data.as_of"] == "2026-07-01"
    assert registry.aliases()[Alias.CHAMPION] == box.champion_version
    assert row["status"] == "completed" and row["outcome"] == "rejected"
    assert row["champion_before"] == row["champion_after"] == box.champion_version


def test_failed_pipeline_is_isolated_and_the_request_can_be_retried(box):
    settings = box.settings()
    registry = box.registry()
    versions_before = {v.version for v in registry.all_versions()}
    box.set_algorithms({"support_vector_machine": {}})  # rejected by config validation in `train`
    try:
        request = new_request(box, date(2026, 7, 1), "broken configuration scenario")
        failed = process_request(settings, registry, request, controller_config(box))
    finally:
        box.set_algorithms(None)

    assert failed.outcome == "failed" and "train" in failed.detail
    assert registry.aliases()[Alias.CHAMPION] == box.champion_version
    assert {v.version for v in registry.all_versions()} == versions_before

    retry = claim_next(settings.ops_database_url, box.model_name, 60, request.request_id)
    retried = process_request(settings, registry, retry, controller_config(box))
    [row] = [r for r in list_requests(settings.ops_database_url, box.model_name) if r["request_id"] == request.request_id]

    assert retried.outcome in {"promoted", "rejected", "awaiting_promotion"}
    assert row["status"] == "completed" and row["attempts"] == 2
    champion = registry.aliases()[Alias.CHAMPION]
    assert champion == (retried.candidate_version if retried.outcome == "promoted" else box.champion_version)


def test_only_one_open_request_and_one_claim_per_request(box):
    settings = box.settings()
    with connect(settings.ops_database_url) as conn:
        first = create_request(conn, box.model_name, "policy", ["drift"], date(2026, 7, 1))
        second = create_request(conn, box.model_name, "policy", ["drift again"], date(2026, 7, 1))

    claimed = claim_next(settings.ops_database_url, box.model_name, 60)
    claimed_again = claim_next(settings.ops_database_url, box.model_name, 60)

    assert first is not None and second is None
    assert claimed.request_id == first and claimed_again is None
    with connect(settings.ops_database_url) as conn:
        conn.execute("UPDATE ops.retraining_requests SET status = 'failed' WHERE request_id = %s", (first,))
