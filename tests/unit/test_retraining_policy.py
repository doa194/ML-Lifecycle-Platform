"""Retraining policy: thresholds, severity, data-quality blocking, cooldown and de-duplication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from churn_platform.config import load_retraining_config
from churn_platform.retraining.policy import Decision, MonitoringSignal, decide
from tests.support.data import REPO_CONFIG

POLICY = load_retraining_config(REPO_CONFIG).policy
NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def signal(**overrides) -> MonitoringSignal:
    values = {"observation_count": 1000}
    values.update(overrides)
    return MonitoringSignal(**values)


def run(sig, last_request_at=None, open_request=False):
    return decide(sig, POLICY, NOW, last_request_at, open_request)


SEVERE_DRIFT = {"dataset_drift": True, "drift_share": 0.4, "prediction_drift": True}


def test_too_few_observations_never_trigger_even_with_severe_drift():
    result = run(signal(observation_count=POLICY.min_observations - 1, **SEVERE_DRIFT))

    assert result.decision == Decision.INSUFFICIENT_DATA


def test_stable_data_needs_no_action():
    assert run(signal()).decision == Decision.NO_ACTION


def test_severe_drift_with_prediction_drift_requests_retraining():
    result = run(signal(**SEVERE_DRIFT))

    assert result.requests_retraining
    assert "drift" in result.reasons[0]


def test_mild_drift_is_only_watched():
    result = run(signal(dataset_drift=False, drift_share=0.1, prediction_drift=True))

    assert result.decision == Decision.WATCH


def test_feature_drift_without_prediction_drift_is_not_enough_by_default():
    # Inputs moved but the model output did not: no evidence the model is affected yet.
    result = run(signal(dataset_drift=True, drift_share=0.5, prediction_drift=False))

    assert result.decision == Decision.WATCH


@pytest.mark.parametrize(("metric", "drop"), [("f1_drop", POLICY.max_f1_drop + 0.01), ("recall_drop", POLICY.max_recall_drop + 0.01)])
def test_delayed_performance_drop_requests_retraining_without_drift(metric, drop):
    result = run(signal(labeled_count=500, **{metric: drop}))

    assert result.requests_retraining
    assert "performance" in result.reasons[0]


def test_drop_exactly_at_the_limit_is_tolerated():
    assert run(signal(labeled_count=500, f1_drop=POLICY.max_f1_drop)).decision == Decision.NO_ACTION


def test_data_quality_issues_block_retraining():
    result = run(signal(data_quality_ok=False, **SEVERE_DRIFT))

    assert result.decision == Decision.BLOCKED
    assert "data quality" in result.reasons[0]


def test_open_request_prevents_a_second_request():
    result = run(signal(**SEVERE_DRIFT), open_request=True)

    assert result.decision == Decision.BLOCKED
    assert result.reasons[-1] == "an open retraining request already exists"


def test_cooldown_blocks_repeated_requests_until_it_expires():
    within = run(signal(**SEVERE_DRIFT), last_request_at=NOW - timedelta(hours=POLICY.cooldown_hours) + timedelta(minutes=1))
    after = run(signal(**SEVERE_DRIFT), last_request_at=NOW - timedelta(hours=POLICY.cooldown_hours))

    assert within.decision == Decision.BLOCKED and "cooldown" in within.reasons[-1]
    assert after.requests_retraining
