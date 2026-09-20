"""Promotion and rollback: the only workflows that move the `champion` alias.

Both are alias changes in the MLflow registry - no model files are copied - so switching
is instant and reversible. Before the alias moves, the target model is loaded and its
fingerprint verified, so a broken or tampered artifact can never become champion. The
inference service picks up the new champion on its next restart (`churnctl serving reload`).
"""

from __future__ import annotations

import logging

from churn_platform.config import QualityGatePolicy
from churn_platform.lifecycle.audit import current_actor, record_event
from churn_platform.lifecycle.registry import ModelRegistry, utc_now
from churn_platform.lifecycle.states import (
    Alias,
    LifecycleError,
    Status,
    VersionState,
    check_transition,
    choose_rollback_target,
    promotion_blockers,
)
from churn_platform.tracking.model_io import load_model

log = logging.getLogger(__name__)


def _verify_loadable(registry: ModelRegistry, state: VersionState) -> None:
    try:
        load_model(registry.model_uri(state.version), expected_fingerprint=state.tags.get("model.fingerprint") or "missing")
    except Exception as error:  # noqa: BLE001 - reported as a refusal, nothing changes
        raise LifecycleError(f"version {state.version} cannot be loaded safely: {error}") from error


def promote(
    registry: ModelRegistry, policy: QualityGatePolicy, ops_url: str, version: str | None = None, approved_by: str | None = None
) -> VersionState:
    target = registry.version_state(version) if version else registry.alias_state(Alias.CHALLENGER)
    if target is None:
        raise LifecycleError("no version holds the 'challenger' alias; run the quality gate first")
    champion = registry.alias_state(Alias.CHAMPION)
    blockers = promotion_blockers(target, champion, policy.approval.manual_approval_required, approved_by)
    if blockers:
        raise LifecycleError("promotion refused:\n  - " + "\n  - ".join(blockers))
    check_transition(target.status, Status.CHAMPION)
    if champion is not None:
        check_transition(champion.status, Status.RETIRED)
    _verify_loadable(registry, target)

    now = utc_now()
    # The alias switch is the single commit point of a promotion.
    registry.set_alias(Alias.CHAMPION, target.version)
    registry.delete_alias(Alias.CHALLENGER)
    registry.set_tags(target.version, {"lifecycle.status": Status.CHAMPION, "lifecycle.promoted_at": now,
                                       "lifecycle.approved_by": approved_by or "policy"})
    if champion is not None:
        registry.set_tags(champion.version, {"lifecycle.status": Status.RETIRED, "lifecycle.retired_at": now})
    record_event(
        ops_url, registry.model_name, target.version, "promoted",
        {"previous_champion": champion.version if champion else None, "approved_by": approved_by or "policy"},
        actor=current_actor(),
    )
    log.info("v%s is now champion (previous: %s)", target.version, champion.version if champion else "none")
    return registry.version_state(target.version)


def rollback(registry: ModelRegistry, ops_url: str, reason: str, to_version: str | None = None) -> VersionState:
    champion = registry.alias_state(Alias.CHAMPION)
    if champion is None:
        raise LifecycleError("there is no champion to roll back")
    target = choose_rollback_target(registry.all_versions(), champion, to_version)
    check_transition(champion.status, Status.ROLLED_BACK)
    check_transition(target.status, Status.CHAMPION)
    _verify_loadable(registry, target)

    now = utc_now()
    registry.set_alias(Alias.CHAMPION, target.version)
    registry.set_tags(champion.version, {"lifecycle.status": Status.ROLLED_BACK, "lifecycle.rolled_back_at": now,
                                         "lifecycle.rollback_reason": reason})
    registry.set_tags(target.version, {"lifecycle.status": Status.CHAMPION, "lifecycle.promoted_at": now,
                                       "lifecycle.restored_by_rollback": "true"})
    record_event(
        ops_url, registry.model_name, target.version, "rolled_back",
        {"from_version": champion.version, "reason": reason}, actor=current_actor(),
    )
    log.info("rolled back champion v%s -> v%s (%s)", champion.version, target.version, reason)
    return registry.version_state(target.version)
