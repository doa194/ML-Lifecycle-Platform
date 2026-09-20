"""Turns a churn probability into the business-facing risk level of the prediction response."""

from __future__ import annotations

from typing import Literal

RiskLevel = Literal["low", "medium", "high"]


def classify_risk(probability: float, decision_threshold: float, medium_ratio: float) -> RiskLevel:
    """`high` exactly when the model predicts churn, so risk level and prediction never disagree."""
    if probability >= decision_threshold:
        return "high"
    if probability >= decision_threshold * medium_ratio:
        return "medium"
    return "low"
