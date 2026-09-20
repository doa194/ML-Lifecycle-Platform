"""Time-aware split: ordering, purge gaps against temporal leakage, and failure modes."""

from __future__ import annotations

import pandas as pd
import pytest

from churn_platform.config import SplitConfig
from churn_platform.data.splitting import SplitError, time_split
from tests.support.data import data_config, small_dataset

ROWS = 8000


@pytest.fixture(scope="module")
def split():
    return time_split(small_dataset(rows=ROWS), data_config().split)


def test_periods_follow_each_other_in_time(split):
    assert split.train["snapshot_date"].max() < split.validation["snapshot_date"].min()
    assert split.validation["snapshot_date"].max() < split.test["snapshot_date"].min()


def test_outcome_windows_never_reach_into_the_next_period(split):
    label_window = pd.Timedelta(days=data_config().generation.label_window_days)

    assert split.train["snapshot_date"].max() + label_window <= split.validation_start
    assert split.validation["snapshot_date"].max() + label_window <= split.test_start


def test_every_row_is_either_assigned_or_purged(split):
    assigned = len(split.train) + len(split.validation) + len(split.test)

    assert assigned + split.purged_rows == ROWS
    assert split.purged_rows > 0


def test_no_customer_appears_in_more_than_one_period(split):
    train, validation, test = (set(f["customer_id"]) for f in (split.train, split.validation, split.test))

    assert train.isdisjoint(validation) and validation.isdisjoint(test) and train.isdisjoint(test)


def test_test_period_holds_the_newest_snapshots(split):
    newest = max(frame["snapshot_date"].max() for frame in (split.train, split.validation, split.test))

    assert split.test["snapshot_date"].max() == newest


def test_too_little_data_for_a_period_fails_loudly():
    config = SplitConfig(validation_days=90, test_days=60, purge_days=30, min_rows_per_split=5000)

    with pytest.raises(SplitError, match="min_rows_per_split"):
        time_split(small_dataset(rows=3000), config)
