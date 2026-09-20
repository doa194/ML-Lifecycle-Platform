"""The `evaluate` pipeline stage: held-out test evaluation of the cycle winner.

The winner is loaded back from MLflow through the verified loader (so this also proves the
stored artifact is loadable and intact), scored on the untouched test period, and
evaluated overall, per customer segment and for single-row latency. Results are logged to
the winner's MLflow run together with the monitoring reference dataset.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

from churn_platform.config import TrainingConfig, load_training_config
from churn_platform.data.schema import FEATURE_COLUMNS, ID_COLUMNS, LABEL_COLUMN
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.pipeline.stages import write_json
from churn_platform.settings import load_settings
from churn_platform.tracking.mlflow_setup import configure_mlflow
from churn_platform.tracking.model_io import LoadedModel, load_model
from churn_platform.training.metrics import (
    add_segment_columns,
    classification_metrics,
    finite_metrics,
    segment_metrics,
    trivial_baseline,
)

log = logging.getLogger(__name__)
REFERENCE_ARTIFACT = "reference/reference.parquet"


def measure_latency(model: LoadedModel, frame: pd.DataFrame, samples: int) -> dict[str, float]:
    """Single-row predict latency (the online serving pattern), in milliseconds."""
    rows = frame.loc[:, list(FEATURE_COLUMNS)].head(samples)
    model.predict_proba(rows.head(1))  # warm-up, excluded from the measurement
    timings = []
    for index in range(len(rows)):
        start = time.perf_counter()
        model.predict_proba(rows.iloc[[index]])
        timings.append((time.perf_counter() - start) * 1000)
    return {
        "p50_ms": float(np.percentile(timings, 50)),
        "p95_ms": float(np.percentile(timings, 95)),
        "max_ms": float(np.max(timings)),
    }


def evaluate_model(model: LoadedModel, frame: pd.DataFrame, config: TrainingConfig) -> dict:
    """Overall + segment metrics, latency and the no-skill baseline on labelled data."""
    probability = model.predict_proba(frame)
    y_true = frame[LABEL_COLUMN].to_numpy()
    segmented = add_segment_columns(frame, config.new_customer_months)
    return {
        "decision_threshold": model.decision_threshold,
        "overall": classification_metrics(y_true, probability, model.decision_threshold),
        "segments": segment_metrics(
            segmented,
            y_true,
            probability,
            model.decision_threshold,
            config.evaluation.segment_columns,
            config.evaluation.min_segment_rows,
        ),
        "latency": measure_latency(model, frame, config.evaluation.latency_samples),
        "trivial_baseline": trivial_baseline(y_true),
    }


def build_reference(model: LoadedModel, frame: pd.DataFrame, max_rows: int) -> pd.DataFrame:
    """Held-out rows with the model's own predictions: the baseline for drift monitoring."""
    reference = frame.loc[:, list(ID_COLUMNS) + list(FEATURE_COLUMNS) + [LABEL_COLUMN]].tail(max_rows).copy()
    reference["churn_probability"] = model.predict_proba(reference)
    reference["churn_prediction"] = (reference["churn_probability"] >= model.decision_threshold).astype(int)
    return reference.reset_index(drop=True)


def run_evaluation_stage() -> int:
    import mlflow

    settings = load_settings()
    paths = PipelinePaths(settings.workspace)
    config = load_training_config(settings.config_dir)
    summary = json.loads(paths.training_summary.read_text(encoding="utf-8"))
    winner = summary["winner"]
    test = pd.read_parquet(paths.split("test"))

    configure_mlflow(settings)
    model = load_model(winner["model_uri"], expected_fingerprint=winner["fingerprint"])
    result = evaluate_model(model, test, config)
    reference = build_reference(model, test, config.reference.max_rows)

    with mlflow.start_run(run_id=winner["run_id"]):
        mlflow.log_metrics(finite_metrics(result["overall"], "test_"))
        mlflow.log_metrics(finite_metrics(result["latency"], "latency_"))
        mlflow.log_metrics(finite_metrics(result["trivial_baseline"], "baseline_"))
        mlflow.log_dict(result["overall"], "evaluation/test_metrics.json")
        mlflow.log_dict({"segments": result["segments"]}, "evaluation/segments.json")
        with tempfile.TemporaryDirectory() as tmp:
            reference_path = Path(tmp) / "reference.parquet"
            reference.to_parquet(reference_path, index=False)
            mlflow.log_artifact(str(reference_path), artifact_path="reference")
        mlflow.set_tags({"evaluation.completed": "true", "evaluation.reference_rows": str(len(reference))})

    report = {
        "cycle_id": summary["cycle_id"],
        "run_id": winner["run_id"],
        "algorithm": winner["algorithm"],
        "model_uri": winner["model_uri"],
        **result,
    }
    write_json(paths.evaluation_report, report)
    overall = result["overall"]
    log.info(
        "test F1=%.3f recall=%.3f ROC-AUC=%.3f (no-skill F1=%.3f), p95 latency %.1f ms",
        overall["f1"], overall["recall"], overall["roc_auc"], result["trivial_baseline"]["f1"], result["latency"]["p95_ms"],
    )
    return 0
