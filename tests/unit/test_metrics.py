"""Metrics, threshold selection, segment evaluation and cycle-winner selection."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from churn_platform.config import ThresholdSearch
from churn_platform.training.cycle import CandidateResult, select_winner
from churn_platform.training.metrics import (
    add_segment_columns,
    classification_metrics,
    segment_metrics,
    select_threshold,
    trivial_baseline,
)


def test_confusion_counts_and_rates():
    metrics = classification_metrics([1, 1, 0, 0, 1], [0.9, 0.2, 0.7, 0.1, 0.6], threshold=0.5)

    assert (metrics["tp"], metrics["fn"], metrics["fp"], metrics["tn"]) == (2, 1, 1, 1)
    assert metrics["recall"] == pytest.approx(2 / 3)
    assert metrics["precision"] == pytest.approx(2 / 3)


def test_ranking_metrics_are_undefined_for_a_single_class():
    metrics = classification_metrics([0, 0, 0], [0.1, 0.4, 0.8], threshold=0.5)

    assert metrics["roc_auc"] is None and metrics["pr_auc"] is None
    assert metrics["f1"] == 0.0


def test_threshold_maximises_f1_on_validation_data():
    y_true = [0, 0, 0, 1, 1, 1]
    probability = [0.10, 0.20, 0.35, 0.40, 0.70, 0.90]

    threshold = select_threshold(y_true, probability, ThresholdSearch(min=0.05, max=0.9, step=0.05))

    # Every threshold in (0.35, 0.40] separates the classes perfectly; the lowest wins ties.
    assert threshold == pytest.approx(0.40)


def test_trivial_baseline_matches_always_predicting_churn():
    y_true = np.array([1, 0, 0, 0])

    baseline = trivial_baseline(y_true)

    assert baseline["f1"] == pytest.approx(classification_metrics(y_true, np.ones(4), 0.5)["f1"])
    assert baseline["roc_auc"] == 0.5


def test_segment_metrics_are_computed_per_segment_value():
    frame = add_segment_columns(pd.DataFrame({"tenure_months": [1, 2, 30, 40], "plan_tier": ["basic"] * 4}), 6)
    y_true = [1, 0, 1, 0]
    probability = [0.9, 0.1, 0.2, 0.1]

    segments = {s["value"]: s for s in segment_metrics(frame, y_true, probability, 0.5, ["tenure_segment"], min_rows=2)}

    assert segments["new"]["recall"] == 1.0
    assert segments["established"]["recall"] == 0.0
    assert all(s["reliable"] for s in segments.values())


def test_small_or_single_class_segments_are_marked_unreliable():
    frame = pd.DataFrame({"contract_type": ["monthly"] * 5 + ["two_year"] * 2})
    y_true = [1, 0, 1, 0, 1, 0, 0]

    segments = {s["value"]: s for s in segment_metrics(frame, y_true, [0.5] * 7, 0.5, ["contract_type"], min_rows=3)}

    assert segments["monthly"]["reliable"] is True
    assert segments["two_year"]["reliable"] is False


def result(algorithm, f1, roc_auc):
    return CandidateResult(algorithm, None, 0.5, {"f1": f1, "roc_auc": roc_auc})


def test_cycle_winner_is_best_f1_with_roc_auc_tie_break():
    results = [result("random_forest", 0.50, 0.80), result("logistic_regression", 0.52, 0.79), result("hist_gradient_boosting", 0.52, 0.83)]

    assert select_winner(results, "f1").algorithm == "hist_gradient_boosting"
