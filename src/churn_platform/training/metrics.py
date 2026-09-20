"""Classification metrics, decision-threshold selection and per-segment evaluation.

F1 is the primary metric: churn is the minority class, so accuracy rewards a model that
never predicts churn. Recall (how many churners we catch) and ROC-AUC (ranking quality
independent of the threshold) are required supporting metrics. Accuracy is recorded for
completeness only and is never used for decisions.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

from churn_platform.config import ThresholdSearch


def classification_metrics(y_true, probability, threshold: float) -> dict[str, float | int | None]:
    probability = np.asarray(probability, dtype=float)
    return metrics_for_predictions(y_true, (probability >= threshold).astype(int), probability)


def metrics_for_predictions(y_true, predicted, probability) -> dict[str, float | int | None]:
    """Metrics for already-made 0/1 predictions (e.g. the predictions production served)."""
    y_true = np.asarray(y_true).astype(int)
    predicted = np.asarray(predicted).astype(int)
    probability = np.asarray(probability, dtype=float)
    tn, fp, fn, tp = confusion_matrix(y_true, predicted, labels=[0, 1]).ravel()
    both_classes = len(np.unique(y_true)) == 2
    return {
        "f1": float(f1_score(y_true, predicted, zero_division=0)),
        "precision": float(precision_score(y_true, predicted, zero_division=0)),
        "recall": float(recall_score(y_true, predicted, zero_division=0)),
        # Ranking metrics are undefined when a sample holds only one class.
        "roc_auc": float(roc_auc_score(y_true, probability)) if both_classes else None,
        "pr_auc": float(average_precision_score(y_true, probability)) if both_classes else None,
        "accuracy": float(accuracy_score(y_true, predicted)),
        "positive_rate": float(predicted.mean()) if len(predicted) else 0.0,
        "churn_rate": float(y_true.mean()) if len(y_true) else 0.0,
        "rows": int(len(y_true)),
        "tp": int(tp),
        "fp": int(fp),
        "tn": int(tn),
        "fn": int(fn),
    }


def select_threshold(y_true, probability, search: ThresholdSearch) -> float:
    """Return the threshold with the highest F1 on the given (validation) data.

    Ties keep the lowest threshold, which favours recall: missing a churner costs more
    than contacting a customer who would have stayed.
    """
    y_true = np.asarray(y_true).astype(int)
    probability = np.asarray(probability, dtype=float)
    candidates = np.round(np.arange(search.min, search.max + search.step / 2, search.step), 4)
    scores = [f1_score(y_true, (probability >= t).astype(int), zero_division=0) for t in candidates]
    return float(candidates[int(np.argmax(scores))])


def trivial_baseline(y_true) -> dict[str, float]:
    """Quality of models without any signal, to prove a trained model learned something.

    Predicting "churn" for everyone maximises recall and gives F1 = 2p / (1 + p), where p is
    the churn rate; a constant score has ROC-AUC 0.5.
    """
    churn_rate = float(np.mean(np.asarray(y_true).astype(int)))
    return {"f1": 2 * churn_rate / (1 + churn_rate) if churn_rate else 0.0, "roc_auc": 0.5}


def add_segment_columns(frame: pd.DataFrame, new_customer_months: int) -> pd.DataFrame:
    """Add evaluation-only segment columns (never used as model features)."""
    out = frame.copy()
    out["tenure_segment"] = np.where(out["tenure_months"] < new_customer_months, "new", "established")
    return out


def segment_metrics(
    frame: pd.DataFrame,
    y_true,
    probability,
    threshold: float,
    segment_columns: list[str],
    min_rows: int,
) -> list[dict]:
    """Metrics for every value of every segment column.

    Segments smaller than `min_rows` are still reported but marked `reliable: False`; the
    quality gate ignores them because metrics on a handful of rows are mostly noise.
    """
    y_true = np.asarray(y_true).astype(int)
    probability = np.asarray(probability, dtype=float)
    results = []
    for column in segment_columns:
        values = frame[column].to_numpy()
        for value in sorted(pd.unique(values)):
            mask = values == value
            metrics = classification_metrics(y_true[mask], probability[mask], threshold)
            results.append(
                {
                    "segment": column,
                    "value": str(value),
                    "reliable": bool(mask.sum() >= min_rows and 0 < y_true[mask].sum() < mask.sum()),
                    **{k: metrics[k] for k in ("rows", "churn_rate", "f1", "recall", "precision", "roc_auc")},
                }
            )
    return results


def finite_metrics(metrics: dict, prefix: str) -> dict[str, float]:
    """Numeric metrics in the flat `prefix_name` form MLflow expects (drops None/NaN)."""
    return {
        f"{prefix}{name}": float(value)
        for name, value in metrics.items()
        if isinstance(value, (int, float)) and value is not None and not math.isnan(float(value))
    }
