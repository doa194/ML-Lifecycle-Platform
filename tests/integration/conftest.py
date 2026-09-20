"""Shared integration fixtures.

`champion_sandbox` trains, registers, gates and promotes one model in an isolated sandbox
once per test session. Tests that only *read* the champion (serving, monitoring, delayed
labels) share it; tests that change aliases build their own sandbox.
"""

from __future__ import annotations

import pytest

from churn_platform.config import load_quality_gate_policy
from churn_platform.lifecycle.gate_runner import run_quality_gate
from churn_platform.lifecycle.promotion import promote
from churn_platform.lifecycle.registration import register_from_workspace
from churn_platform.pipeline.paths import PipelinePaths
from tests.support.lifecycle import LifecycleSandbox


@pytest.fixture(scope="session")
def champion_sandbox(tmp_path_factory, stack):
    box = LifecycleSandbox(tmp_path_factory.mktemp("champion")).create()
    box.repro()
    settings = box.settings()
    registry = box.registry()
    version = register_from_workspace(registry, PipelinePaths(box.root), settings.ops_database_url).version.version
    decision = run_quality_gate(registry, box.root, settings.config_dir, settings.ops_database_url, version)
    assert decision.passed, decision.to_dict()
    promote(registry, load_quality_gate_policy(settings.config_dir), settings.ops_database_url)
    box.champion_version = version
    yield box
    box.cleanup()
