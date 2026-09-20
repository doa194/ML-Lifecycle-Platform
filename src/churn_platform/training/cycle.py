"""The `train` pipeline stage: one training cycle = three algorithms, one winner.

MLflow layout per cycle:
    training cycle run (parent)        lineage files: params.yaml, config, dvc.lock
      |- logistic_regression run       params, validation metrics, model artifact
      |- random_forest run             ...
      '- hist_gradient_boosting run    ...

The winner is chosen on the validation period only (the test period stays untouched until
the evaluate stage). Winning makes a model a *candidate*, nothing more: this stage never
registers models or touches registry aliases. Promotion is a separate, gated workflow.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from churn_platform.config import TrainingConfig, load_training_config
from churn_platform.data.schema import FEATURE_COLUMNS, LABEL_COLUMN
from churn_platform.features.preprocessing import build_model_pipeline
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.pipeline.stages import write_json
from churn_platform.settings import load_settings
from churn_platform.tracking.lineage import base_lineage_tags
from churn_platform.tracking.mlflow_setup import configure_mlflow
from churn_platform.tracking.model_io import log_model
from churn_platform.training.algorithms import build_classifier
from churn_platform.training.metrics import (
    classification_metrics,
    finite_metrics,
    select_threshold,
)

log = logging.getLogger(__name__)
LINEAGE_FILES = ("params.yaml", "dvc.lock", "config/data.yaml", "config/training.yaml")


@dataclass
class CandidateResult:
    algorithm: str
    pipeline: Any
    decision_threshold: float
    validation_metrics: dict[str, Any]


def fit_candidate(algorithm: str, config: TrainingConfig, train: pd.DataFrame, validation: pd.DataFrame) -> CandidateResult:
    classifier = build_classifier(algorithm, config.algorithms[algorithm], config.random_state)
    pipeline = build_model_pipeline(classifier, config.new_customer_months)
    pipeline.fit(train.loc[:, list(FEATURE_COLUMNS)], train[LABEL_COLUMN])
    probability = pipeline.predict_proba(validation.loc[:, list(FEATURE_COLUMNS)])[:, 1]
    threshold = select_threshold(validation[LABEL_COLUMN], probability, config.threshold_search)
    return CandidateResult(
        algorithm=algorithm,
        pipeline=pipeline,
        decision_threshold=threshold,
        validation_metrics=classification_metrics(validation[LABEL_COLUMN], probability, threshold),
    )


def select_winner(results: list[CandidateResult], metric: str) -> CandidateResult:
    """Best selection metric wins; ROC-AUC breaks ties, then algorithm name (deterministic)."""
    if not results:
        raise ValueError("no candidates to choose from")

    def rank(result: CandidateResult):
        return (result.validation_metrics[metric] or 0.0, result.validation_metrics["roc_auc"] or 0.0, result.algorithm)

    return max(results, key=rank)


def run_training_stage() -> int:
    import mlflow

    settings = load_settings()
    workspace = settings.workspace
    paths = PipelinePaths(workspace)
    config = load_training_config(settings.config_dir)
    train = pd.read_parquet(paths.split("train"))
    validation = pd.read_parquet(paths.split("validation"))

    configure_mlflow(settings)
    experiment = mlflow.set_experiment(settings.experiment_name)
    lineage = base_lineage_tags(workspace)
    cycle_id = f"cycle-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    cycle_tags = {**lineage, "training.cycle_id": cycle_id}

    candidates = []
    with mlflow.start_run(run_name=cycle_id, tags={**cycle_tags, "training.role": "cycle"}) as parent:
        for name in LINEAGE_FILES:
            if (workspace / name).exists():
                mlflow.log_artifact(str(workspace / name), artifact_path="lineage")
        mlflow.log_params({"train_rows": len(train), "validation_rows": len(validation), "algorithms": ",".join(config.algorithms)})

        for algorithm in config.algorithms:
            log.info("training %s", algorithm)
            with mlflow.start_run(
                run_name=algorithm,
                nested=True,
                tags={**cycle_tags, "training.role": "candidate_run", "training.algorithm": algorithm},
            ) as child:
                result = fit_candidate(algorithm, config, train, validation)
                mlflow.log_params(
                    {
                        **{f"model.{k}": v for k, v in config.algorithms[algorithm].items()},
                        "algorithm": algorithm,
                        "decision_threshold": result.decision_threshold,
                        "random_state": config.random_state,
                        "new_customer_months": config.new_customer_months,
                    }
                )
                mlflow.log_metrics(finite_metrics(result.validation_metrics, "val_"))
                info, fingerprint = log_model(result.pipeline, result.decision_threshold, algorithm, validation.head(5))
                mlflow.set_tags({"model.fingerprint": fingerprint, "model.uri": info.model_uri})
                candidates.append((result, child.info.run_id, info.model_uri, fingerprint))
                log.info("%s: val F1=%.3f recall=%.3f ROC-AUC=%.3f threshold=%.2f", algorithm,
                         result.validation_metrics["f1"], result.validation_metrics["recall"],
                         result.validation_metrics["roc_auc"], result.decision_threshold)

        winner = select_winner([c[0] for c in candidates], config.selection_metric)
        winner_result, winner_run_id, winner_uri, winner_fingerprint = next(c for c in candidates if c[0] is winner)
        mlflow.set_tags({"training.winner_run_id": winner_run_id, "training.winner_algorithm": winner.algorithm})
        mlflow.log_metrics(finite_metrics(winner.validation_metrics, "winner_val_"))

    summary = {
        "cycle_id": cycle_id,
        "experiment_id": experiment.experiment_id,
        "cycle_run_id": parent.info.run_id,
        "selection_metric": config.selection_metric,
        "lineage": lineage,
        "candidates": [
            {
                "algorithm": result.algorithm,
                "run_id": run_id,
                "model_uri": uri,
                "fingerprint": fingerprint,
                "decision_threshold": result.decision_threshold,
                "validation": {k: result.validation_metrics[k] for k in ("f1", "recall", "precision", "roc_auc")},
            }
            for result, run_id, uri, fingerprint in candidates
        ],
        "winner": {
            "algorithm": winner.algorithm,
            "run_id": winner_run_id,
            "model_uri": winner_uri,
            "fingerprint": winner_fingerprint,
            "decision_threshold": winner.decision_threshold,
        },
    }
    write_json(paths.training_summary, summary)
    log.info("cycle %s winner: %s (run %s)", cycle_id, winner.algorithm, winner_run_id)
    log.debug(json.dumps(summary, indent=2))
    return 0
