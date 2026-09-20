"""Dataset validation: leakage columns, domain constraints and label maturity are rejected."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from churn_platform.data.schema import (
    FEATURE_COLUMNS,
    ID_COLUMNS,
    LABEL_COLUMN,
    find_leakage_columns,
)
from churn_platform.data.validation import validate_dataset
from tests.support.data import AS_OF, data_config, small_dataset


@pytest.fixture(scope="module")
def valid_frame():
    return small_dataset(rows=6000)


def failed_checks(frame, as_of=AS_OF):
    return {check.name for check in validate_dataset(frame, data_config(), as_of).failures}


@pytest.mark.parametrize(
    "column",
    ["cancellation_request_date", "cancellation_status", "churn_date", "account_closed", "Exit_Survey_Score", "days_until_churn"],
)
def test_outcome_leaking_columns_are_detected(column):
    assert find_leakage_columns([*FEATURE_COLUMNS, column]) == [column]


def test_label_identifiers_and_features_are_not_flagged_as_leakage():
    assert find_leakage_columns([*ID_COLUMNS, *FEATURE_COLUMNS, LABEL_COLUMN]) == []


def test_model_features_never_include_label_or_identifiers():
    assert not set(FEATURE_COLUMNS) & {LABEL_COLUMN, *ID_COLUMNS}


def test_valid_dataset_passes(valid_frame):
    assert failed_checks(valid_frame) == set()


def test_dataset_with_leakage_column_is_rejected(valid_frame):
    frame = valid_frame.assign(cancellation_status="none")

    assert {"no_outcome_leakage_columns", "schema_columns"} <= failed_checks(frame)


def corrupt(frame, column, value, rows=5):
    frame = frame.copy()
    if value is None or isinstance(value, str):
        frame[column] = frame[column].astype(object)
    frame.loc[frame.index[:rows], column] = value
    return frame


@pytest.mark.parametrize(
    ("column", "value", "expected_check"),
    [
        ("tenure_months", -1, "numeric_ranges"),
        ("monthly_charge", 5000.0, "numeric_ranges"),
        ("plan_tier", "platinum", "categorical_domains"),
        ("contract_type", None, "required_values_present"),
        (LABEL_COLUMN, 2, "binary_label"),
    ],
)
def test_domain_violations_are_reported(valid_frame, column, value, expected_check):
    frame = corrupt(valid_frame, column, value)

    assert expected_check in failed_checks(frame)


def test_complaints_cannot_exceed_tickets(valid_frame):
    frame = valid_frame.copy()
    rows = frame.index[:3]
    frame.loc[rows, "complaints_90d"] = frame.loc[rows, "support_tickets_90d"] + 1

    assert "complaints_within_tickets" in failed_checks(frame)


def test_resolution_time_without_tickets_is_inconsistent(valid_frame):
    no_tickets = valid_frame.index[valid_frame["support_tickets_90d"] == 0][:3]
    frame = valid_frame.copy()
    frame.loc[no_tickets, "avg_resolution_hours"] = 12.0

    assert "resolution_time_only_with_tickets" in failed_checks(frame)


def test_duplicate_customer_snapshots_are_rejected(valid_frame):
    frame = valid_frame.copy()
    frame.loc[frame.index[1], "customer_id"] = frame.loc[frame.index[0], "customer_id"]
    frame.loc[frame.index[1], "snapshot_date"] = frame.loc[frame.index[0], "snapshot_date"]

    assert "unique_customer_snapshots" in failed_checks(frame)


def test_snapshots_without_closed_outcome_window_are_rejected(valid_frame):
    # Declaring an earlier extract date means the newest labels could not be known yet.
    assert "labels_matured" in failed_checks(valid_frame, as_of=date(2025, 11, 1))


def test_implausible_churn_rate_is_rejected(valid_frame):
    frame = valid_frame.assign(**{LABEL_COLUMN: np.int8(0)})

    assert "churn_rate_plausible" in failed_checks(frame)
