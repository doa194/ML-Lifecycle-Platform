"""Quality-gate decisions: every check, champion comparison and segment regressions."""

from __future__ import annotations

import pytest

from churn_platform.config import load_quality_gate_policy
from churn_platform.lifecycle.quality_gate import ModelEvaluation, evaluate_gate
from tests.support.data import REPO_CONFIG

POLICY = load_quality_gate_policy(REPO_CONFIG)


def segment(column, value, f1, recall, reliable=True):
    return {"segment": column, "value": value, "f1": f1, "recall": recall, "reliable": reliable}


def evaluation(version="2", f1=0.55, recall=0.65, roc_auc=0.85, latency=8.0, segments=None, load_error=None):
    return ModelEvaluation(
        version=version,
        overall={"f1": f1, "recall": recall, "roc_auc": roc_auc, "accuracy": 0.8},
        segments=segments
        if segments is not None
        else [segment("plan_tier", "basic", 0.55, 0.66), segment("contract_type", "annual", 0.40, 0.45)],
        latency_p95_ms=latency,
        load_error=load_error,
    )


def failed(decision):
    return {check.name for check in decision.failed_checks}


def test_strong_candidate_without_champion_passes_on_absolute_checks():
    decision = evaluate_gate(evaluation(), None, POLICY)

    assert decision.passed
    assert "champion_comparison" in {c.name for c in decision.checks}


@pytest.mark.parametrize(
    ("overrides", "expected_failure"),
    [
        ({"f1": 0.30}, "f1_min"),
        ({"recall": 0.40}, "recall_min"),
        ({"roc_auc": 0.70}, "roc_auc_min"),
        ({"roc_auc": None}, "roc_auc_min"),
        ({"latency": 120.0}, "latency_p95_ms"),
        ({"latency": None}, "latency_p95_ms"),
    ],
)
def test_each_absolute_threshold_can_reject_a_candidate(overrides, expected_failure):
    decision = evaluate_gate(evaluation(**overrides), None, POLICY)

    assert not decision.passed
    assert failed(decision) == {expected_failure}


def test_threshold_boundary_counts_as_pass():
    decision = evaluate_gate(evaluation(f1=POLICY.absolute.f1_min, recall=POLICY.absolute.recall_min), None, POLICY)

    assert decision.passed


def test_unloadable_or_tampered_model_fails_before_any_metric_check():
    decision = evaluate_gate(evaluation(load_error="ModelIntegrityError: fingerprint mismatch"), None, POLICY)

    assert [c.name for c in decision.checks] == ["loadable_and_intact"]
    assert not decision.passed


def test_weak_reliable_segment_rejects_the_candidate():
    segments = [segment("plan_tier", "basic", 0.6, 0.7), segment("contract_type", "two_year", 0.2, 0.10)]

    decision = evaluate_gate(evaluation(segments=segments), None, POLICY)

    assert failed(decision) == {"segment_recall_min"}


def test_unreliable_or_unmonitored_segments_are_not_enforced():
    segments = [
        segment("contract_type", "two_year", 0.1, 0.05, reliable=False),
        segment("region", "north", 0.1, 0.05),  # not a configured critical segment column
    ]

    assert evaluate_gate(evaluation(segments=segments), None, POLICY).passed


def test_candidate_within_tolerance_of_champion_passes():
    champion = evaluation(version="1", f1=0.555)

    decision = evaluate_gate(evaluation(f1=0.55), champion, POLICY)

    assert decision.passed
    assert decision.champion_version == "1"


@pytest.mark.parametrize(("metric", "check"), [("f1", "f1_vs_champion"), ("roc_auc", "roc_auc_vs_champion")])
def test_regression_against_champion_rejects_the_candidate(metric, check):
    champion = evaluation(version="1", **{metric: 0.90})

    decision = evaluate_gate(evaluation(**{metric: 0.86}), champion, POLICY)

    assert check in failed(decision)


def test_overall_improvement_cannot_hide_a_segment_regression():
    champion = evaluation(
        version="1", f1=0.50, segments=[segment("plan_tier", "basic", 0.50, 0.60), segment("contract_type", "annual", 0.50, 0.60)]
    )
    candidate = evaluation(
        f1=0.60, segments=[segment("plan_tier", "basic", 0.70, 0.80), segment("contract_type", "annual", 0.35, 0.40)]
    )

    decision = evaluate_gate(candidate, champion, POLICY)

    assert failed(decision) == {"segment_f1_vs_champion"}


def test_segment_recall_traded_for_precision_is_not_a_regression():
    # The champion over-predicted churn in a segment (high recall, poor precision); a
    # candidate with better F1 there is not rejected for catching a few fewer churners,
    # as long as the absolute recall floor still holds.
    champion = evaluation(version="1", f1=0.50, segments=[segment("plan_tier", "basic", 0.50, 0.80)])
    candidate = evaluation(f1=0.58, segments=[segment("plan_tier", "basic", 0.58, 0.55)])

    assert evaluate_gate(candidate, champion, POLICY).passed


def test_accuracy_never_influences_the_decision():
    weak = evaluation(f1=0.30)
    weak.overall["accuracy"] = 0.99

    assert not evaluate_gate(weak, None, POLICY).passed
