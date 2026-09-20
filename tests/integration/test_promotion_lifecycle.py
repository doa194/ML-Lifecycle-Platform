"""Governance against the real registry: gate outcomes, promotion and rollback move aliases
exactly as the rules say, and a failed candidate can never change the champion.
"""

from __future__ import annotations

import pytest

from churn_platform.config import load_quality_gate_policy
from churn_platform.lifecycle.audit import recent_events
from churn_platform.lifecycle.gate_runner import run_quality_gate
from churn_platform.lifecycle.promotion import promote, rollback
from churn_platform.lifecycle.registration import register_from_workspace
from churn_platform.lifecycle.states import Alias, LifecycleError, Status
from churn_platform.pipeline.paths import PipelinePaths
from tests.support.lifecycle import WEAK_ALGORITHMS, LifecycleSandbox

pytestmark = pytest.mark.integration


def register_and_gate(box):
    settings = box.settings()
    registry = box.registry()
    version = register_from_workspace(registry, PipelinePaths(box.root), settings.ops_database_url).version.version
    decision = run_quality_gate(registry, box.root, settings.config_dir, settings.ops_database_url, version)
    return version, decision


@pytest.fixture(scope="module")
def box(tmp_path_factory, stack):
    sandbox = LifecycleSandbox(tmp_path_factory.mktemp("promotion")).create()
    sandbox.repro()
    version, decision = register_and_gate(sandbox)
    assert decision.passed and decision.champion_version is None
    promote(sandbox.registry(), load_quality_gate_policy(sandbox.settings().config_dir), sandbox.settings().ops_database_url)
    sandbox.first_champion = version
    yield sandbox
    sandbox.cleanup()


def test_weak_candidate_is_rejected_and_the_champion_is_untouched(box):
    box.set_algorithms(WEAK_ALGORITHMS)
    try:
        box.repro()
        weak_version, decision = register_and_gate(box)
    finally:
        box.set_algorithms(None)
    registry = box.registry()
    weak = registry.version_state(weak_version)

    assert not decision.passed
    assert {"f1_min", "roc_auc_min"} & {c.name for c in decision.failed_checks}
    assert registry.aliases()[Alias.CHAMPION] == box.first_champion
    assert Alias.CHALLENGER not in registry.aliases()
    assert weak.status == Status.REJECTED and weak.tags["gate.champion_version"] == box.first_champion
    with pytest.raises(LifecycleError):
        promote(registry, load_quality_gate_policy(box.settings().config_dir), box.settings().ops_database_url, weak_version)
    assert registry.aliases()[Alias.CHAMPION] == box.first_champion


def test_promotion_and_rollback_only_move_the_champion_alias(box):
    settings = box.settings()
    registry = box.registry()
    box.retrain()
    new_version, decision = register_and_gate(box)
    assert decision.passed and decision.champion_version == box.first_champion

    promote(registry, load_quality_gate_policy(settings.config_dir), settings.ops_database_url)
    after_promotion = registry.aliases()
    retired = registry.version_state(box.first_champion)

    rollback(registry, settings.ops_database_url, reason="integration test rollback")
    after_rollback = registry.aliases()
    events = [e["event_type"] for e in recent_events(settings.ops_database_url, box.model_name)]

    assert after_promotion[Alias.CHAMPION] == new_version and Alias.CHALLENGER not in after_promotion
    assert retired.status == Status.RETIRED
    assert after_rollback[Alias.CHAMPION] == box.first_champion
    assert registry.version_state(new_version).status == Status.ROLLED_BACK
    assert registry.version_state(box.first_champion).status == Status.CHAMPION
    assert events[:2] == ["rolled_back", "promoted"]
