"""Dataset validation rules, applied by the `validate` pipeline stage before any training.

Each rule produces a named check result instead of raising on the first problem, so the
validation report lists every issue at once. The pipeline stage fails when any check
fails, which stops DVC before bad data can reach `prepare`, `train` or the registry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta

import pandas as pd

from churn_platform.config import DataConfig
from churn_platform.data.schema import (
    BOOLEAN_FEATURES,
    CATEGORY_LEVELS,
    DATASET_COLUMNS,
    ID_COLUMNS,
    LABEL_COLUMN,
    NULLABLE_FEATURES,
    NUMERIC_RANGES,
    find_leakage_columns,
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ValidationReport:
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": [asdict(check) for check in self.checks]}


def validate_dataset(df: pd.DataFrame, config: DataConfig, as_of: date) -> ValidationReport:
    checks: list[CheckResult] = []

    def check(name: str, passed: bool, failure_detail: str) -> None:
        checks.append(CheckResult(name, bool(passed), "ok" if passed else failure_detail))

    leaking = find_leakage_columns(df.columns)
    check("no_outcome_leakage_columns", not leaking, f"prohibited post-snapshot columns: {leaking}")
    missing = [c for c in DATASET_COLUMNS if c not in df.columns]
    unexpected = [c for c in df.columns if c not in DATASET_COLUMNS]
    check("schema_columns", not missing and not unexpected, f"missing={missing} unexpected={unexpected}")
    if missing:
        # The remaining rules need the full schema; report what we have so far.
        return ValidationReport(checks)

    check("minimum_rows", len(df) >= config.validation.min_rows, f"{len(df)} < {config.validation.min_rows}")
    duplicates = int(df.duplicated(list(ID_COLUMNS)).sum())
    check("unique_customer_snapshots", duplicates == 0, f"{duplicates} duplicate (customer_id, snapshot_date) rows")

    required = [c for c in DATASET_COLUMNS if c not in NULLABLE_FEATURES]
    null_counts = {c: int(n) for c, n in df[required].isna().sum().items() if n}
    check("required_values_present", not null_counts, f"nulls in required columns: {null_counts}")

    bad_levels = {
        column: sorted(set(df[column].dropna().unique()) - set(levels))
        for column, levels in CATEGORY_LEVELS.items()
    }
    bad_levels = {c: v for c, v in bad_levels.items() if v}
    check("categorical_domains", not bad_levels, f"unknown categories: {bad_levels}")

    bad_bools = [c for c in BOOLEAN_FEATURES if not df[c].dropna().isin([True, False]).all()]
    check("boolean_domains", not bad_bools, f"non-boolean values in {bad_bools}")

    out_of_range = {}
    for column, (low, high) in NUMERIC_RANGES.items():
        values = df[column].dropna()
        count = int(((values < low) | (values > high)).sum())
        if count:
            out_of_range[column] = count
    check("numeric_ranges", not out_of_range, f"out-of-range values: {out_of_range}")

    check(
        "complaints_within_tickets",
        bool((df["complaints_90d"] <= df["support_tickets_90d"]).all()),
        "complaints_90d exceeds support_tickets_90d",
    )
    has_tickets = df["support_tickets_90d"] > 0
    check(
        "resolution_time_only_with_tickets",
        bool((df["avg_resolution_hours"].isna() == ~has_tickets).all()),
        "avg_resolution_hours must be null exactly when there are no tickets",
    )

    check("binary_label", bool(df[LABEL_COLUMN].isin([0, 1]).all()), f"{LABEL_COLUMN} must be 0 or 1")
    churn_rate = float(df[LABEL_COLUMN].mean())
    check(
        "churn_rate_plausible",
        config.validation.churn_rate_min <= churn_rate <= config.validation.churn_rate_max,
        f"churn rate {churn_rate:.3f} outside [{config.validation.churn_rate_min}, {config.validation.churn_rate_max}]",
    )

    # A label is only real once its outcome window has closed on the extract date.
    latest_allowed = pd.Timestamp(as_of - timedelta(days=config.generation.label_window_days))
    immature = int((pd.to_datetime(df["snapshot_date"]) > latest_allowed).sum())
    check("labels_matured", immature == 0, f"{immature} snapshots after {latest_allowed.date()} have unknown outcomes")
    return ValidationReport(checks)
