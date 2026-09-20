"""`churnctl monitor`: run monitoring cycles once, as a long-running worker, or show results.

The monitoring worker container runs `churnctl monitor run --loop`. A failing cycle (for
example MLflow briefly unreachable) is logged and retried on the next interval; the worker
itself keeps running and never affects the inference service.
"""

from __future__ import annotations

import argparse
import json
import logging
import time

from churn_platform.cli.common import mlflow_settings, print_table

log = logging.getLogger(__name__)


def register(groups: argparse._SubParsersAction) -> None:
    monitor = groups.add_parser("monitor", help="ML monitoring (drift, data quality, delayed performance)")
    actions = monitor.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run", help="run a monitoring cycle now")
    run.add_argument("--loop", action="store_true", help="keep running cycles (worker mode)")
    run.add_argument("--interval", type=float, help="seconds between cycles (default from monitoring.yaml)")
    run.set_defaults(handler=run_monitor)
    status = actions.add_parser("status", help="show the latest monitoring runs")
    status.add_argument("--limit", type=int, default=10)
    status.set_defaults(handler=show_status)


def _cycle(settings, skip_if_unchanged: bool) -> int:
    from churn_platform.config import load_monitoring_config, load_retraining_config
    from churn_platform.monitoring.runner import SKIPPED, run_monitoring_cycle

    result = run_monitoring_cycle(
        settings, load_monitoring_config(settings.config_dir), load_retraining_config(settings.config_dir).policy,
        skip_if_unchanged=skip_if_unchanged,
    )
    if result.decision == SKIPPED:
        log.info("no new predictions or outcomes for v%s since the last run; cycle skipped", result.model_version)
        return 0
    performance = result.performance
    print(
        json.dumps(
            {
                "model_version": result.model_version,
                "observations": result.observation_count,
                "dataset_drift": result.drift.dataset_drift if result.drift else None,
                "drift_share": result.drift.drift_share if result.drift else None,
                "drifted_features": result.drift.drifted_features if result.drift else None,
                "prediction_drift": result.drift.prediction_drift if result.drift else None,
                "data_quality_ok": result.quality.ok if result.quality else None,
                "labeled": performance.labeled_count if performance else 0,
                "production_performance": performance.metrics if performance else None,
                "f1_drop_vs_test": performance.f1_drop if performance else None,
                "decision": str(result.decision),
                "reasons": result.reasons,
                "retraining_request_id": str(result.retraining_request_id) if result.retraining_request_id else None,
            },
            indent=2,
        )
    )
    return 0


def run_monitor(args: argparse.Namespace) -> int:
    from churn_platform.config import load_monitoring_config

    settings = mlflow_settings()
    if not args.loop:
        return _cycle(settings, skip_if_unchanged=False)
    interval = args.interval or load_monitoring_config(settings.config_dir).worker.interval_seconds
    log.info("monitoring worker started: one cycle every %.0f seconds", interval)
    while True:
        try:
            # The worker only analyses when data changed; a manual run always analyses.
            _cycle(settings, skip_if_unchanged=True)
        except Exception:  # noqa: BLE001 - a failed cycle must not stop the worker
            log.exception("monitoring cycle failed; retrying in %.0f seconds", interval)
        time.sleep(interval)


def show_status(args: argparse.Namespace) -> int:
    from churn_platform.storage.db import connect

    settings = mlflow_settings()
    with connect(settings.ops_database_url) as conn:
        rows = conn.execute(
            "SELECT finished_at, model_version, observation_count, drift_share, dataset_drift, prediction_drift, "
            "labeled_count, performance->>'f1', policy_decision FROM ops.monitoring_runs WHERE model_name = %s "
            "ORDER BY finished_at DESC LIMIT %s",
            (settings.model_name, args.limit),
        ).fetchall()
    table = [
        {"finished_utc": r[0].strftime("%Y-%m-%d %H:%M:%S"), "version": r[1], "observations": r[2], "drift_share": r[3],
         "dataset_drift": r[4], "prediction_drift": r[5], "labeled": r[6],
         "production_f1": f"{float(r[7]):.3f}" if r[7] else "-", "decision": r[8]}
        for r in rows
    ]
    print_table(table, ["finished_utc", "version", "observations", "drift_share", "dataset_drift", "prediction_drift",
                        "labeled", "production_f1", "decision"])
    return 0
