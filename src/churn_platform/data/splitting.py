"""Time-aware train/validation/test split with purging.

A random split would let a model train on customers observed *after* the ones it is
evaluated on, which overstates quality for a forecasting task. Instead:

    |-------- train --------|purge|--- validation ---|purge|--- test ---|
    oldest snapshot                                          newest snapshot

* test       = the newest `test_days` of snapshots
* validation = the `validation_days` before the test period
* purge      = rows whose outcome window (snapshot + label window) would reach into the
               next period are dropped. Otherwise a training label could "see" what
               happened during the validation period (temporal leakage).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from churn_platform.config import SplitConfig
from churn_platform.data.schema import LABEL_COLUMN


class SplitError(ValueError):
    """Raised when the data cannot produce a valid time-aware split."""


@dataclass(frozen=True)
class TimeSplit:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
    validation_start: pd.Timestamp
    test_start: pd.Timestamp
    purged_rows: int

    def report(self) -> dict:
        def summary(frame: pd.DataFrame) -> dict:
            return {
                "rows": len(frame),
                "first_snapshot": str(frame["snapshot_date"].min().date()),
                "last_snapshot": str(frame["snapshot_date"].max().date()),
                "churn_rate": round(float(frame[LABEL_COLUMN].mean()), 4),
            }

        return {
            "validation_start": str(self.validation_start.date()),
            "test_start": str(self.test_start.date()),
            "purged_rows": self.purged_rows,
            "train": summary(self.train),
            "validation": summary(self.validation),
            "test": summary(self.test),
        }


def time_split(df: pd.DataFrame, config: SplitConfig) -> TimeSplit:
    dates = pd.to_datetime(df["snapshot_date"])
    newest = dates.max()
    test_start = newest - pd.Timedelta(days=config.test_days - 1)
    validation_start = test_start - pd.Timedelta(days=config.validation_days)
    purge = pd.Timedelta(days=config.purge_days)

    # A row may join a period only if its outcome window closes before the next period starts.
    train_mask = dates + purge <= validation_start
    validation_mask = (dates >= validation_start) & (dates + purge <= test_start)
    test_mask = dates >= test_start

    split = TimeSplit(
        train=df[train_mask].reset_index(drop=True),
        validation=df[validation_mask].reset_index(drop=True),
        test=df[test_mask].reset_index(drop=True),
        validation_start=validation_start,
        test_start=test_start,
        purged_rows=int((~(train_mask | validation_mask | test_mask)).sum()),
    )
    too_small = {
        name: len(frame)
        for name, frame in (("train", split.train), ("validation", split.validation), ("test", split.test))
        if len(frame) < config.min_rows_per_split
    }
    if too_small:
        raise SplitError(f"split periods below min_rows_per_split={config.min_rows_per_split}: {too_small}")
    return split
