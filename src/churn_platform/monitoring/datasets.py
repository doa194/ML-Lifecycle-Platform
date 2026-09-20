"""Builds the two datasets monitoring compares.

reference - the held-out test rows of the model version being monitored, with that model's
            own predictions. It is logged with the model at training time, so it is always
            the exact data the served model was validated on (traceable via the run ID).
current   - the newest production observations served by that same model version.

Comparing each version only with its own reference means a freshly promoted model starts
with a clean monitoring history instead of inheriting drift from its predecessor.
"""

from __future__ import annotations

import tempfile

import mlflow
import pandas as pd
import psycopg

from churn_platform.data.schema import (
    BOOLEAN_FEATURES,
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    INTEGER_FEATURES,
    NUMERIC_FEATURES,
)

REFERENCE_ARTIFACT = "reference/reference.parquet"
META_COLUMNS = ("prediction_id", "predicted_at", "customer_id", "snapshot_date", "churn_probability", "churn_prediction", "actual_churn")
_SELECT = (
    "SELECT prediction_id, predicted_at, customer_id, snapshot_date, churn_probability, churn_prediction, actual_churn, features "
    "FROM ops.prediction_observations "
)


def normalize_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Give feature columns the same dtypes as the training data (JSON loses them)."""
    out = frame.copy()
    for column in CATEGORICAL_FEATURES:
        out[column] = out[column].astype(str)
    for column in BOOLEAN_FEATURES:
        out[column] = out[column].astype(bool)
    for column in NUMERIC_FEATURES:
        out[column] = pd.to_numeric(out[column]).astype("int64" if column in INTEGER_FEATURES else "float64")
    return out


def observations_frame(rows: list[tuple]) -> pd.DataFrame:
    """Rows from `_SELECT` -> one frame with metadata and typed feature columns, oldest first."""
    if not rows:
        return pd.DataFrame(columns=[*META_COLUMNS, *FEATURE_COLUMNS])
    meta = pd.DataFrame([row[:7] for row in rows], columns=list(META_COLUMNS))
    features = pd.DataFrame([row[7] for row in rows]).reindex(columns=list(FEATURE_COLUMNS))
    frame = pd.concat([meta, normalize_features(features)], axis=1)
    frame["churn_probability"] = frame["churn_probability"].astype(float)
    return frame.sort_values("predicted_at").reset_index(drop=True)


def latest_served_version(conn: psycopg.Connection, model_name: str) -> str | None:
    """The model version behind the newest prediction (what production is running now)."""
    row = conn.execute(
        "SELECT model_version FROM ops.prediction_observations WHERE model_name = %s ORDER BY predicted_at DESC LIMIT 1",
        (model_name,),
    ).fetchone()
    return row[0] if row else None


def current_window(conn: psycopg.Connection, model_name: str, model_version: str, limit: int) -> pd.DataFrame:
    rows = conn.execute(
        _SELECT + "WHERE model_name = %s AND model_version = %s ORDER BY predicted_at DESC LIMIT %s",
        (model_name, model_version, limit),
    ).fetchall()
    return observations_frame(rows)


def labeled_window(conn: psycopg.Connection, model_name: str, model_version: str, limit: int) -> pd.DataFrame:
    """Newest observations of this version whose true outcome is already known."""
    rows = conn.execute(
        _SELECT + "WHERE model_name = %s AND model_version = %s AND actual_churn IS NOT NULL "
        "ORDER BY predicted_at DESC LIMIT %s",
        (model_name, model_version, limit),
    ).fetchall()
    return observations_frame(rows)


def load_reference(run_id: str) -> pd.DataFrame:
    with tempfile.TemporaryDirectory() as tmp:
        path = mlflow.artifacts.download_artifacts(run_id=run_id, artifact_path=REFERENCE_ARTIFACT, dst_path=tmp)
        return normalize_features(pd.read_parquet(path))
