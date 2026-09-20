"""Synthetic data generation: determinism, snapshot semantics and profile behaviour."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from churn_platform.data.generator import generate_dataset, simulate_customers
from churn_platform.data.schema import DATASET_COLUMNS, LABEL_COLUMN
from churn_platform.data.validation import validate_dataset
from tests.support.data import AS_OF, data_config


def test_same_inputs_reproduce_identical_data():
    first = generate_dataset(data_config(), AS_OF, seed=11, rows=2000)
    second = generate_dataset(data_config(), AS_OF, seed=11, rows=2000)

    pd.testing.assert_frame_equal(first, second)


def test_different_seed_produces_different_customers():
    first = generate_dataset(data_config(), AS_OF, seed=11, rows=2000)
    second = generate_dataset(data_config(), AS_OF, seed=12, rows=2000)

    assert not first[["tenure_months", "monthly_charge"]].equals(second[["tenure_months", "monthly_charge"]])
    assert set(first["customer_id"]).isdisjoint(second["customer_id"])


def test_generated_data_satisfies_schema_and_domain_rules():
    frame = generate_dataset(data_config(), AS_OF, seed=3, rows=6000)

    report = validate_dataset(frame, data_config(), AS_OF)

    assert list(frame.columns) == list(DATASET_COLUMNS)
    assert report.passed, report.failures


def test_only_snapshots_with_known_outcomes_are_extracted():
    frame = generate_dataset(data_config(), AS_OF, seed=3, rows=2000)
    label_window = data_config().generation.label_window_days

    assert frame["snapshot_date"].max() <= pd.Timestamp(AS_OF - timedelta(days=label_window))
    assert frame[LABEL_COLUMN].isin([0, 1]).all()


def test_timeline_switches_profile_at_the_configured_date():
    # The default timeline switches to `pricing_shift` on 2026-01-01; an extract taken in
    # mid-2026 contains snapshots from both regimes.
    frame = generate_dataset(data_config(), date(2026, 7, 1), seed=5, rows=8000)
    before = frame[frame["snapshot_date"] < "2026-01-01"]
    after = frame[frame["snapshot_date"] >= "2026-01-01"]

    assert after["recent_price_increase"].mean() > before["recent_price_increase"].mean() + 0.2
    assert after[LABEL_COLUMN].mean() > before[LABEL_COLUMN].mean()


@pytest.mark.parametrize(
    ("profile", "column", "direction"),
    [("pricing_shift", "recent_price_increase", 1), ("engagement_shift", "monthly_usage_hours", -1)],
)
def test_shift_profiles_move_their_target_features(profile, column, direction):
    baseline = simulate_customers(np.random.default_rng(21), np.array(["baseline"] * 5000), data_config())
    shifted = simulate_customers(np.random.default_rng(21), np.array([profile] * 5000), data_config())

    change = shifted.features[column].astype(float).mean() - baseline.features[column].astype(float).mean()

    assert np.sign(change) == direction
