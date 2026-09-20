"""Retraining controller: turns one retraining request into a governed model update.

    claim request -> move dataset as-of date -> dvc repro (all 3 algorithms) -> dvc push
      -> register candidate -> quality gate -> promote (only if allowed) -> close request

Guarantees:
  * The champion alias can only move in the final promotion step, and only for a candidate
    that passed the quality gate. Any failure earlier leaves production untouched.
  * Every step is safe to repeat: DVC skips stages whose inputs did not change,
    registration returns the existing version for an already registered run, and the
    gate/promotion re-check the registry state. A failed request can therefore simply be
    retried (`churnctl retrain run --request-id ...`).
  * Serving is not touched: the inference service keeps its loaded model until it is
    explicitly reloaded.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from churn_platform.config import (
    RetrainingConfig,
    load_dataset_params,
    load_quality_gate_policy,
)
from churn_platform.lifecycle.gate_runner import run_quality_gate
from churn_platform.lifecycle.promotion import promote
from churn_platform.lifecycle.registration import register_from_workspace
from churn_platform.lifecycle.registry import ModelRegistry
from churn_platform.lifecycle.states import Alias
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.retraining.requests import RetrainingRequest, finish
from churn_platform.settings import Settings

log = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """A DVC command failed; its output is kept for the request's error field."""


@dataclass(frozen=True)
class RetrainingOutcome:
    request_id: str
    outcome: str  # promoted | awaiting_promotion | rejected | failed
    candidate_version: str | None
    champion_before: str | None
    champion_after: str | None
    detail: str = ""


def set_dataset_as_of(workspace: Path, as_of: date) -> bool:
    """Point params.yaml at a new data-warehouse extract date; returns True if it changed.

    A targeted line edit keeps the file's comments, and the result is re-read through the
    normal loader so a malformed edit fails immediately.
    """
    path = workspace / "params.yaml"
    text = path.read_text(encoding="utf-8")
    if load_dataset_params(workspace).as_of == as_of:
        return False
    updated, count = re.subn(r'^(\s*as_of:\s*).*$', rf'\g<1>"{as_of.isoformat()}"', text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError("params.yaml has no dataset.as_of entry")
    path.write_text(updated, encoding="utf-8")
    if load_dataset_params(workspace).as_of != as_of:
        raise ValueError("params.yaml update did not take effect")
    return True


def run_dvc(settings: Settings, *args: str) -> None:
    env = dict(os.environ)
    # Stages run `python`, which must be this environment's interpreter, and they must log
    # to the same experiment / registry names as the controller.
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env["CHURN_WORKSPACE"] = str(settings.workspace)
    env["CHURN_MODEL_NAME"] = settings.model_name
    env["CHURN_EXPERIMENT_NAME"] = settings.experiment_name
    result = subprocess.run(
        [sys.executable, "-m", "dvc", *args], cwd=settings.workspace, env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-15:])
        raise PipelineError(f"dvc {' '.join(args)} failed:\n{tail}")


def _gate_and_maybe_promote(
    settings: Settings, registry: ModelRegistry, config: RetrainingConfig, candidate: str, champion: str | None
) -> tuple[str, str]:
    if candidate == champion:
        # DVC reused every stage: the champion was already trained on exactly this data.
        return "unchanged", "pipeline inputs unchanged; the champion already reflects this data"
    decision = run_quality_gate(registry, settings.workspace, settings.config_dir, settings.ops_database_url, candidate)
    if not decision.passed:
        return "rejected", "quality gate failed: " + ", ".join(c.name for c in decision.failed_checks)
    policy = load_quality_gate_policy(settings.config_dir)
    if config.controller.auto_promote and not policy.approval.manual_approval_required:
        promote(registry, policy, settings.ops_database_url, candidate)
        return "promoted", "passed the quality gate and was promoted"
    return "awaiting_promotion", "passed the quality gate; promotion needs an operator"


def process_request(
    settings: Settings, registry: ModelRegistry, request: RetrainingRequest, config: RetrainingConfig
) -> RetrainingOutcome:
    champion = registry.alias_state(Alias.CHAMPION)
    champion_before = champion.version if champion else None
    log.info("retraining request %s (%s): data as of %s", request.request_id, request.trigger, request.data_as_of)
    try:
        if set_dataset_as_of(settings.workspace, request.data_as_of):
            log.info("dataset as-of moved to %s", request.data_as_of)
        run_dvc(settings, "repro")
        if config.controller.push_data:
            run_dvc(settings, "push")
        candidate = register_from_workspace(registry, PipelinePaths(settings.workspace), settings.ops_database_url).version
        outcome, detail = _gate_and_maybe_promote(settings, registry, config, candidate.version, champion_before)
    except Exception as error:  # noqa: BLE001 - any failure closes the request as failed
        log.error("retraining request %s failed: %s", request.request_id, error)
        after = registry.alias_state(Alias.CHAMPION)
        finish(settings.ops_database_url, request.request_id, "failed", error=str(error)[:2000],
               champion_version_before=champion_before, champion_version_after=after.version if after else None)
        return RetrainingOutcome(str(request.request_id), "failed", None, champion_before,
                                 after.version if after else None, str(error))

    after = registry.alias_state(Alias.CHAMPION)
    champion_after = after.version if after else None
    finish(settings.ops_database_url, request.request_id, "completed", outcome=outcome,
           candidate_version=candidate.version, champion_version_before=champion_before,
           champion_version_after=champion_after)
    log.info("request %s: candidate v%s %s (champion %s -> %s)", request.request_id, candidate.version, outcome,
             champion_before, champion_after)
    return RetrainingOutcome(str(request.request_id), outcome, candidate.version, champion_before, champion_after, detail)
