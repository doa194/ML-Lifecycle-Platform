"""Model lifecycle rules: aliases, statuses, allowed transitions, promotion and rollback.

This module is pure logic (no MLflow calls) so every rule is unit-testable.

Aliases in the MLflow registry are the source of truth for "which version plays which role":
    candidate  - the winner of the most recent training cycle
    challenger - a candidate that passed the quality gate and may replace the champion
    champion   - the approved production model the inference service loads

Each version also carries a `lifecycle.status` tag that records where it is in its life:
    candidate -> challenger -> champion -> retired      (normal path)
             '-> rejected                             (failed quality gate)
    champion -> rolled_back                             (replaced by a rollback)
    retired  -> champion                                (rollback target restored)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Alias(StrEnum):
    CANDIDATE = "candidate"
    CHALLENGER = "challenger"
    CHAMPION = "champion"


class Status(StrEnum):
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    CHALLENGER = "challenger"
    CHAMPION = "champion"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


# Allowed status changes. Re-running the gate on a candidate, rejected or challenger version
# is allowed (policies can change); a version that was ever champion is never re-gated.
_TRANSITIONS: dict[Status | None, set[Status]] = {
    None: {Status.CANDIDATE},
    Status.CANDIDATE: {Status.CHALLENGER, Status.REJECTED},
    Status.REJECTED: {Status.CHALLENGER, Status.REJECTED},
    Status.CHALLENGER: {Status.CHALLENGER, Status.REJECTED, Status.CHAMPION},
    Status.CHAMPION: {Status.RETIRED, Status.ROLLED_BACK},
    Status.RETIRED: {Status.CHAMPION},
    Status.ROLLED_BACK: set(),
}


class LifecycleError(RuntimeError):
    """A requested lifecycle change is not allowed; nothing was changed."""


def check_transition(current: Status | None, target: Status) -> None:
    if target not in _TRANSITIONS[current]:
        raise LifecycleError(f"transition {current or 'unregistered'} -> {target} is not allowed")


@dataclass(frozen=True)
class VersionState:
    """What the registry says about one model version (aliases + lifecycle tags)."""

    version: str
    aliases: frozenset[str] = frozenset()
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> Status | None:
        value = self.tags.get("lifecycle.status")
        return Status(value) if value else None

    @property
    def gate_passed(self) -> bool:
        return self.tags.get("gate.status") == "passed"

    def timestamp(self, tag: str) -> datetime | None:
        value = self.tags.get(tag)
        return datetime.fromisoformat(value) if value else None


NO_CHAMPION = "none"


def promotion_blockers(
    version: VersionState,
    current_champion: VersionState | None,
    manual_approval_required: bool,
    approved_by: str | None,
) -> list[str]:
    """Every reason why `version` may not become champion right now (empty = allowed)."""
    blockers = []
    if current_champion is not None and current_champion.version == version.version:
        blockers.append(f"version {version.version} is already the champion")
        return blockers
    if Alias.CHALLENGER not in version.aliases:
        blockers.append(f"version {version.version} does not hold the '{Alias.CHALLENGER}' alias")
    if not version.gate_passed:
        blockers.append(f"version {version.version} has not passed the quality gate")
    # The gate compared the challenger with a specific champion. If the champion changed
    # since then (promotion or rollback), that comparison is stale and must be redone.
    expected = current_champion.version if current_champion else NO_CHAMPION
    compared_with = version.tags.get("gate.champion_version")
    if compared_with != expected:
        blockers.append(
            f"quality gate compared against champion '{compared_with}', current champion is '{expected}': re-run the gate"
        )
    if manual_approval_required and not approved_by:
        blockers.append("manual approval is required: pass --approved-by <name>")
    return blockers


def choose_rollback_target(
    versions: list[VersionState], current_champion: VersionState, requested_version: str | None
) -> VersionState:
    """Pick the version to restore as champion.

    Default: the most recently retired champion (the model that served right before the
    current one). An explicitly requested version must be a retired former champion that
    passed the gate; rolled-back and never-promoted versions are refused.
    """
    eligible = [
        v
        for v in versions
        if v.version != current_champion.version and v.status == Status.RETIRED and v.gate_passed
    ]
    if requested_version is not None:
        match = next((v for v in eligible if v.version == requested_version), None)
        if match is None:
            raise LifecycleError(
                f"version {requested_version} is not a retired, gate-approved former champion; eligible: "
                f"{sorted((v.version for v in eligible), key=int) or 'none'}"
            )
        return match
    if not eligible:
        raise LifecycleError("no previous validated champion to roll back to")
    oldest = datetime.min.replace(tzinfo=UTC)
    return max(eligible, key=lambda v: (v.timestamp("lifecycle.retired_at") or oldest, int(v.version)))
