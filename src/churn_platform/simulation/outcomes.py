"""Delayed ground truth: reveals churn outcomes for predictions whose outcome window closed.

At prediction time nobody knows whether the customer will churn. The outcome becomes known
only after the 30-day window following the snapshot. This workflow plays the role of the
billing system that reports those outcomes later: for every unresolved prediction whose
window has closed on the given date, it copies the hidden simulated outcome into the
observation. Performance metrics (precision, recall, F1, ROC-AUC) can only be computed for
observations resolved this way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from churn_platform.storage.db import connect


def outcome_is_resolvable(snapshot_date: date, as_of: date, label_window_days: int) -> bool:
    """An outcome is known only once the full window after the snapshot has passed."""
    return snapshot_date + timedelta(days=label_window_days) <= as_of


@dataclass(frozen=True)
class ResolutionSummary:
    resolved: int
    still_pending: int
    no_outcome_available: int


def resolve_outcomes(ops_url: str, as_of: date, label_window_days: int, model_name: str | None = None) -> ResolutionSummary:
    with connect(ops_url) as conn:
        pending = conn.execute(
            "SELECT o.prediction_id, o.snapshot_date, s.churned "
            "FROM ops.prediction_observations o "
            "LEFT JOIN simulation.customer_outcomes s "
            "  ON s.customer_id = o.customer_id AND s.snapshot_date = o.snapshot_date "
            "WHERE o.actual_churn IS NULL AND (%s::text IS NULL OR o.model_name = %s)",
            (model_name, model_name),
        ).fetchall()
        matured = [row for row in pending if outcome_is_resolvable(row[1], as_of, label_window_days)]
        known = [(row[2], row[0]) for row in matured if row[2] is not None]
        with conn.cursor() as cursor:
            cursor.executemany(
                "UPDATE ops.prediction_observations SET actual_churn = %s, outcome_resolved_at = now() "
                "WHERE prediction_id = %s AND actual_churn IS NULL",
                known,
            )
    return ResolutionSummary(
        resolved=len(known),
        still_pending=len(pending) - len(matured),
        no_outcome_available=len(matured) - len(known),
    )
