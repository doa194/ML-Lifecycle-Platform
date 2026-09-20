"""Dataset preparation with pandas: normalise column order and types, order rows by time.

Preparation only makes the validated extract consistent; it does not create model
features. Feature engineering lives inside the scikit-learn pipeline
(`churn_platform.features`), so exactly the same transformations run during training and
inference and cannot drift apart.
"""

from __future__ import annotations

import pandas as pd

from churn_platform.data.schema import (
    BOOLEAN_FEATURES,
    CATEGORICAL_FEATURES,
    DATASET_COLUMNS,
    INTEGER_FEATURES,
    LABEL_COLUMN,
    NUMERIC_FEATURES,
)


def prepare_dataset(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw[list(DATASET_COLUMNS)].copy()
    df["customer_id"] = df["customer_id"].astype(str)
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.normalize()
    for column in CATEGORICAL_FEATURES:
        df[column] = df[column].astype(str)
    for column in BOOLEAN_FEATURES:
        df[column] = df[column].astype(bool)
    for column in NUMERIC_FEATURES:
        df[column] = df[column].astype("int64" if column in INTEGER_FEATURES else "float64")
    df[LABEL_COLUMN] = df[LABEL_COLUMN].astype("int8")
    return df.sort_values(["snapshot_date", "customer_id"]).reset_index(drop=True)
