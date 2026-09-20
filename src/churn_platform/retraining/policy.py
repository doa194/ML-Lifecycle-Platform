"""Retraining policy: does the latest monitoring evidence justify a retraining request?

Pure logic, so every threshold, the cooldown and the de-duplication rule are unit-tested.

Evidence and how it is weighed:
  * too few observations                -> no decision possible ("insufficient_data")
  * data quality problems               -> blocked: fix the data first, do not learn from it
  * performance drop (needs outcomes)   -> trigger: the strongest signal
  * severe drift + prediction drift     -> trigger: many inputs moved and the model's
                                           output moved with them
  * milder drift                        -> "watch": recorded, but no retraining
Triggers are then suppressed by an already open request (no pile-up) or the cooldown
(no retraining loops while the same drift keeps being reported).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from churn_platform.config import RetrainingPolicy


class Decision(StrEnum):
    INSUFFICIENT_DATA = "insufficient_data"
    NO_ACTION = "no_action"
    WATCH = "watch"
    BLOCKED = "blocked"
    REQUEST_RETRAINING = "request_retraining"


@dataclass(frozen=True)
class MonitoringSignal:
    observation_count: int
    dataset_drift: bool = False
    drift_share: float = 0.0
    prediction_drift: bool = False
    data_quality_ok: bool = True
    labeled_count: int = 0
    f1_drop: float | None = None  # reference F1 - production F1 (None without enough labels)
    recall_drop: float | None = None


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reasons: list[str] = field(default_factory=list)

    @property
    def requests_retraining(self) -> bool:
        return self.decision == Decision.REQUEST_RETRAINING


def decide(
    signal: MonitoringSignal,
    policy: RetrainingPolicy,
    now: datetime,
    last_request_at: datetime | None,
    open_request_exists: bool,
) -> PolicyDecision:
    if signal.observation_count < policy.min_observations:
        return PolicyDecision(
            Decision.INSUFFICIENT_DATA,
            [f"only {signal.observation_count} observations; the policy needs {policy.min_observations}"],
        )
    if not signal.data_quality_ok and policy.block_on_data_quality_issues:
        return PolicyDecision(Decision.BLOCKED, ["data quality issues in the current window; investigate before retraining"])

    triggers = []
    if signal.f1_drop is not None and signal.f1_drop > policy.max_f1_drop:
        triggers.append(f"performance: F1 dropped by {signal.f1_drop:.3f} (limit {policy.max_f1_drop})")
    if signal.recall_drop is not None and signal.recall_drop > policy.max_recall_drop:
        triggers.append(f"performance: recall dropped by {signal.recall_drop:.3f} (limit {policy.max_recall_drop})")

    severe = signal.drift_share >= policy.severe_drift_share
    if severe and (signal.prediction_drift or not policy.require_prediction_drift):
        triggers.append(
            f"drift: {signal.drift_share:.0%} of features drifted"
            + (" and the prediction distribution shifted" if signal.prediction_drift else "")
        )

    if not triggers:
        if signal.dataset_drift or signal.prediction_drift or signal.drift_share > 0:
            return PolicyDecision(
                Decision.WATCH,
                [f"drift below retraining severity ({signal.drift_share:.0%} of features, prediction drift={signal.prediction_drift})"],
            )
        return PolicyDecision(Decision.NO_ACTION, ["no drift and no performance degradation"])

    if open_request_exists:
        return PolicyDecision(Decision.BLOCKED, [*triggers, "an open retraining request already exists"])
    if last_request_at is not None and now - last_request_at < timedelta(hours=policy.cooldown_hours):
        until = last_request_at + timedelta(hours=policy.cooldown_hours)
        return PolicyDecision(Decision.BLOCKED, [*triggers, f"cooldown active until {until.isoformat(timespec='minutes')}"])
    return PolicyDecision(Decision.REQUEST_RETRAINING, triggers)
