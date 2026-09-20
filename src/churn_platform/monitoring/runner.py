"""One monitoring cycle, run by the monitoring worker container or `churnctl monitor run`.

    newest observations of the served version  --+
                                                  +--> Evidently drift + data quality
    that version's reference (from MLflow)     --+
    resolved outcomes of that version          -----> delayed performance
                                                       |
                            retraining policy  <-------+
                                   |
            monitoring_runs row (+ retraining request if justified) in PostgreSQL
            HTML/JSON report as an MLflow run in the "<experiment>-monitoring" experiment

Monitoring observes and records. It may create a retraining *request*; it never trains,
registers, promotes or deploys anything.
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from mlflow.tracking import MlflowClient

from churn_platform.config import MonitoringConfig, RetrainingPolicy
from churn_platform.monitoring import analysis, datasets
from churn_platform.retraining import policy as retraining_policy
from churn_platform.retraining import requests as retraining_requests
from churn_platform.settings import Settings
from churn_platform.storage.db import connect

log = logging.getLogger(__name__)


@dataclass
class MonitoringResult:
    monitoring_run_id: uuid.UUID
    model_version: str | None
    observation_count: int
    decision: str
    reasons: list[str]
    drift: analysis.DriftSummary | None = None
    quality: analysis.DataQualitySummary | None = None
    performance: analysis.PerformanceSummary | None = None
    retraining_request_id: uuid.UUID | None = None


def monitoring_experiment(settings: Settings) -> str:
    return f"{settings.experiment_name}-monitoring"


SKIPPED = "skipped_no_new_data"


def input_fingerprint(conn, model_name: str, version: str) -> str:
    """Changes whenever a prediction is added or an outcome is resolved for this version."""
    count, newest, labeled, newest_label = conn.execute(
        "SELECT count(*), max(predicted_at), count(actual_churn), max(outcome_resolved_at) "
        "FROM ops.prediction_observations WHERE model_name = %s AND model_version = %s",
        (model_name, version),
    ).fetchone()
    return f"v{version}|{count}|{newest}|{labeled}|{newest_label}"


def last_input_fingerprint(conn, model_name: str) -> str | None:
    row = conn.execute(
        "SELECT input_fingerprint FROM ops.monitoring_runs WHERE model_name = %s ORDER BY finished_at DESC LIMIT 1",
        (model_name,),
    ).fetchone()
    return row[0] if row else None


def run_monitoring_cycle(
    settings: Settings,
    config: MonitoringConfig,
    policy: RetrainingPolicy,
    now: datetime | None = None,
    skip_if_unchanged: bool = False,
) -> MonitoringResult:
    started = datetime.now(UTC)
    now = now or started
    run_id = uuid.uuid4()
    client = MlflowClient(settings.mlflow_tracking_uri)

    with connect(settings.ops_database_url) as conn:
        version = datasets.latest_served_version(conn, settings.model_name)
        if version is None:
            log.info("no predictions recorded for %s yet; nothing to monitor", settings.model_name)
            return MonitoringResult(run_id, None, 0, retraining_policy.Decision.INSUFFICIENT_DATA, ["no observations yet"])
        fingerprint = input_fingerprint(conn, settings.model_name, version)
        if skip_if_unchanged and fingerprint == last_input_fingerprint(conn, settings.model_name):
            # Same data as the previous run -> same results; do not store a duplicate report.
            return MonitoringResult(run_id, version, 0, SKIPPED, ["no new predictions or outcomes since the last run"])
        current = datasets.current_window(conn, settings.model_name, version, config.window.max_observations)
        labeled = datasets.labeled_window(conn, settings.model_name, version, config.performance.max_labeled)

    model_version = client.get_model_version(settings.model_name, version)
    reference_run_id = model_version.run_id
    reference_dataset = (model_version.tags or {}).get("data.id", "unknown")
    run_metrics = client.get_run(reference_run_id).data.metrics
    reference_performance = {k: run_metrics.get(f"test_{k}") for k in ("f1", "recall", "precision", "roc_auc")}

    result = MonitoringResult(run_id, version, len(current), retraining_policy.Decision.INSUFFICIENT_DATA, [])
    result.performance = analysis.summarize_performance(labeled, reference_performance, config.performance.min_labeled)
    snapshot = None
    if len(current) < config.window.min_observations:
        result.reasons = [f"{len(current)} observations; drift needs at least {config.window.min_observations}"]
    else:
        reference = datasets.load_reference(reference_run_id)
        snapshot, metrics = analysis.run_evidently(reference, current, config.drift)
        result.drift = analysis.summarize_drift(analysis.drift_scores(metrics), config.drift)
        result.quality = analysis.summarize_quality(current, analysis.missing_shares(metrics), reference, config.data_quality)

    with connect(settings.ops_database_url) as conn:
        # One transaction: the decision, the optional request and the run record agree.
        if result.drift is not None:
            signal = retraining_policy.MonitoringSignal(
                observation_count=len(current),
                dataset_drift=result.drift.dataset_drift,
                drift_share=result.drift.drift_share,
                prediction_drift=result.drift.prediction_drift,
                data_quality_ok=result.quality.ok,
                labeled_count=result.performance.labeled_count,
                f1_drop=result.performance.f1_drop,
                recall_drop=result.performance.recall_drop,
            )
            decision = retraining_policy.decide(
                signal,
                policy,
                now,
                retraining_requests.last_request_time(conn, settings.model_name),
                retraining_requests.open_request_exists(conn, settings.model_name),
            )
            result.decision, result.reasons = decision.decision, decision.reasons
            if decision.requests_retraining:
                # Retrain on the warehouse extract as of the newest production snapshot.
                data_as_of = max(current["snapshot_date"])
                result.retraining_request_id = retraining_requests.create_request(
                    conn, settings.model_name, "policy", decision.reasons, data_as_of, run_id
                )
                if result.retraining_request_id is None:
                    result.decision = retraining_policy.Decision.BLOCKED
                    result.reasons = [*decision.reasons, "an open retraining request already exists"]
        _persist(conn, settings, result, started, current, reference_run_id, reference_dataset, fingerprint)

    # The report upload can be slow, so it happens after the database transaction committed.
    report_run_id = _log_report(settings, result, snapshot, reference_run_id)
    if report_run_id:
        with connect(settings.ops_database_url) as conn:
            conn.execute(
                "UPDATE ops.monitoring_runs SET report_run_id = %s WHERE monitoring_run_id = %s",
                (report_run_id, result.monitoring_run_id),
            )

    log.info(
        "monitoring v%s: %d observations, drift share %s, prediction drift %s, labeled %d -> %s",
        version, len(current), result.drift.drift_share if result.drift else "n/a",
        result.drift.prediction_drift if result.drift else "n/a", result.performance.labeled_count, result.decision,
    )
    return result


def _persist(conn, settings, result, started, current, reference_run_id, reference_dataset, fingerprint) -> None:
    drift = result.drift
    performance = result.performance
    conn.execute(
        """
        INSERT INTO ops.monitoring_runs (
            monitoring_run_id, started_at, finished_at, model_name, model_version, reference_run_id, reference_dataset,
            observation_count, window_start, window_end, data_quality, dataset_drift, drift_share, drifted_features,
            feature_drift_scores, prediction_drift, prediction_drift_score, labeled_count, performance,
            reference_performance, policy_decision, policy_reasons, retraining_request_id, input_fingerprint)
        VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            result.monitoring_run_id, started, settings.model_name, result.model_version, reference_run_id, reference_dataset,
            result.observation_count,
            current["predicted_at"].min() if len(current) else None,
            current["predicted_at"].max() if len(current) else None,
            json.dumps(result.quality.to_dict() if result.quality else {}),
            drift.dataset_drift if drift else None,
            drift.drift_share if drift else None,
            json.dumps(drift.drifted_features if drift else []),
            json.dumps(drift.feature_scores if drift else {}),
            drift.prediction_drift if drift else None,
            drift.prediction_drift_score if drift else None,
            performance.labeled_count,
            json.dumps(performance.metrics) if performance.metrics else None,
            json.dumps(performance.reference),
            str(result.decision),
            json.dumps(result.reasons),
            result.retraining_request_id,
            fingerprint,
        ),
    )


def _log_report(settings: Settings, result: MonitoringResult, snapshot, reference_run_id: str) -> str | None:
    """Keep the full Evidently report and the summary as an MLflow run (best effort)."""
    import mlflow

    summary = {
        "monitoring_run_id": str(result.monitoring_run_id),
        "model_version": result.model_version,
        "decision": str(result.decision),
        "reasons": result.reasons,
        "drift": asdict(result.drift) if result.drift else None,
        "data_quality": result.quality.to_dict() if result.quality else None,
        "performance": asdict(result.performance) if result.performance else None,
    }
    try:
        mlflow.set_experiment(monitoring_experiment(settings))
        tags = {"monitoring.model_name": settings.model_name, "monitoring.model_version": str(result.model_version),
                "monitoring.reference_run_id": reference_run_id, "monitoring.decision": str(result.decision)}
        with mlflow.start_run(run_name=f"monitor-v{result.model_version}", tags=tags) as run:
            metrics = {"observation_count": result.observation_count, "labeled_count": result.performance.labeled_count}
            if result.drift:
                metrics |= {"drift_share": result.drift.drift_share, "prediction_drift_score": result.drift.prediction_drift_score}
            if result.performance.metrics:
                metrics |= {f"production_{k}": v for k, v in result.performance.metrics.items() if v is not None}
            mlflow.log_metrics(metrics)
            mlflow.log_dict(summary, "monitoring/summary.json")
            if snapshot is not None:
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "evidently_report.html"
                    snapshot.save_html(str(path))
                    mlflow.log_artifact(str(path), artifact_path="monitoring")
        return run.info.run_id
    except Exception as error:  # noqa: BLE001 - the report is supplementary; the DB record is not
        log.warning("monitoring report not stored in MLflow: %s", error)
        return None
