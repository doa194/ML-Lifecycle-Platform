"""Lifecycle state rules: allowed transitions, promotion eligibility and rollback targets."""

from __future__ import annotations

import pytest

from churn_platform.lifecycle.registration import registration_problems
from churn_platform.lifecycle.states import (
    NO_CHAMPION,
    Alias,
    LifecycleError,
    Status,
    VersionState,
    check_transition,
    choose_rollback_target,
    promotion_blockers,
)
from churn_platform.tracking.lineage import REQUIRED_LINEAGE_TAGS


def version(number, status=None, aliases=(), **tags) -> VersionState:
    all_tags = {k.replace("__", "."): v for k, v in tags.items()}
    if status:
        all_tags["lifecycle.status"] = status
    return VersionState(str(number), frozenset(aliases), all_tags)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (None, Status.CANDIDATE),
        (Status.CANDIDATE, Status.CHALLENGER),
        (Status.CANDIDATE, Status.REJECTED),
        (Status.REJECTED, Status.CHALLENGER),  # re-gated after a policy change
        (Status.CHALLENGER, Status.CHAMPION),
        (Status.CHAMPION, Status.RETIRED),
        (Status.CHAMPION, Status.ROLLED_BACK),
        (Status.RETIRED, Status.CHAMPION),
    ],
)
def test_allowed_transitions(current, target):
    check_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (None, Status.CHAMPION),  # nothing skips registration and the gate
        (Status.CANDIDATE, Status.CHAMPION),  # a candidate must pass the gate first
        (Status.REJECTED, Status.CHAMPION),
        (Status.CHAMPION, Status.CHALLENGER),  # a former champion is never re-gated
        (Status.ROLLED_BACK, Status.CHAMPION),  # a rolled-back model stays out
        (Status.RETIRED, Status.CHALLENGER),
    ],
)
def test_forbidden_transitions(current, target):
    with pytest.raises(LifecycleError):
        check_transition(current, target)


def eligible_challenger(number="2", compared_with="1"):
    return version(number, Status.CHALLENGER, [Alias.CHALLENGER], gate__status="passed", gate__champion_version=compared_with)


def test_gated_challenger_may_replace_the_champion_it_was_compared_with():
    champion = version("1", Status.CHAMPION, [Alias.CHAMPION])

    assert promotion_blockers(eligible_challenger(), champion, manual_approval_required=False, approved_by=None) == []


def test_first_champion_needs_a_gate_run_without_champion():
    challenger = eligible_challenger(number="1", compared_with=NO_CHAMPION)

    assert promotion_blockers(challenger, None, manual_approval_required=False, approved_by=None) == []


def test_challenger_compared_with_an_older_champion_is_stale():
    champion = version("3", Status.CHAMPION, [Alias.CHAMPION])

    blockers = promotion_blockers(eligible_challenger(compared_with="1"), champion, False, None)

    assert any("re-run the gate" in b for b in blockers)


@pytest.mark.parametrize(
    "candidate",
    [
        version("2", Status.REJECTED, [], gate__status="failed", gate__champion_version="1"),
        version("2", Status.CANDIDATE, [Alias.CANDIDATE]),
    ],
)
def test_versions_without_a_passed_gate_cannot_be_promoted(candidate):
    champion = version("1", Status.CHAMPION, [Alias.CHAMPION])

    blockers = promotion_blockers(candidate, champion, False, None)

    assert any("challenger" in b for b in blockers)
    assert any("quality gate" in b for b in blockers)


def test_manual_approval_switch_requires_an_approver():
    champion = version("1", Status.CHAMPION, [Alias.CHAMPION])

    assert promotion_blockers(eligible_challenger(), champion, True, None) == [
        "manual approval is required: pass --approved-by <name>"
    ]
    assert promotion_blockers(eligible_challenger(), champion, True, "alice") == []


def test_promoting_the_current_champion_is_refused():
    champion = version("1", Status.CHAMPION, [Alias.CHAMPION])

    assert promotion_blockers(champion, champion, False, None) == ["version 1 is already the champion"]


def history():
    return [
        version("1", Status.RETIRED, gate__status="passed", lifecycle__retired_at="2026-02-01T00:00:00+00:00"),
        version("2", Status.RETIRED, gate__status="passed", lifecycle__retired_at="2026-03-01T00:00:00+00:00"),
        version("3", Status.ROLLED_BACK, gate__status="passed"),
        version("4", Status.REJECTED, gate__status="failed"),
        version("5", Status.CHAMPION, [Alias.CHAMPION], gate__status="passed"),
    ]


def test_rollback_defaults_to_the_most_recently_retired_champion():
    versions = history()

    assert choose_rollback_target(versions, versions[-1], None).version == "2"


def test_rollback_to_an_explicit_older_champion():
    versions = history()

    assert choose_rollback_target(versions, versions[-1], "1").version == "1"


@pytest.mark.parametrize("requested", ["3", "4", "5", "99"])
def test_rollback_refuses_rolled_back_rejected_current_or_unknown_versions(requested):
    versions = history()

    with pytest.raises(LifecycleError):
        choose_rollback_target(versions, versions[-1], requested)


def test_rollback_without_any_previous_champion_is_refused():
    only_champion = [version("1", Status.CHAMPION, [Alias.CHAMPION], gate__status="passed")]

    with pytest.raises(LifecycleError, match="no previous validated champion"):
        choose_rollback_target(only_champion, only_champion[0], None)


def complete_run_tags():
    return {tag: "value" for tag in REQUIRED_LINEAGE_TAGS} | {"evaluation.completed": "true"}


COMPLETE_METRICS = {"val_f1": 0.5, "test_f1": 0.5, "test_recall": 0.6, "test_roc_auc": 0.8, "latency_p95_ms": 5.0}


def test_complete_finished_run_may_be_registered():
    assert registration_problems("FINISHED", complete_run_tags(), COMPLETE_METRICS) == []


def test_registration_requires_finished_traceable_and_evaluated_runs():
    tags = complete_run_tags()
    del tags["data.train_md5"], tags["evaluation.completed"]
    metrics = {k: v for k, v in COMPLETE_METRICS.items() if k != "test_recall"}

    problems = registration_problems("FAILED", tags, metrics)

    assert len(problems) == 4
    assert any("data.train_md5" in p for p in problems)
    assert any("test_recall" in p for p in problems)
