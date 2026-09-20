"""`churnctl retrain`: create, process and inspect retraining requests.

Requests normally come from the monitoring policy; `request` lets an operator ask for one
manually (it still respects "one open request per model"). `run` is the retraining
controller: it processes the next pending request in this workspace, because the DVC
pipeline and dataset versions live here.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from churn_platform.cli.common import mlflow_settings, print_table, registry_for


def register(groups: argparse._SubParsersAction) -> None:
    retrain = groups.add_parser("retrain", help="continuous training: retraining requests and controller")
    actions = retrain.add_subparsers(dest="action", required=True)

    request = actions.add_parser("request", help="create a manual retraining request")
    request.add_argument("--reason", required=True)
    request.add_argument("--data-as-of", type=date.fromisoformat,
                         help="dataset extract date (default: newest production snapshot, else current params)")
    request.set_defaults(handler=create_manual_request)

    run = actions.add_parser("run", help="process the next pending retraining request")
    run.add_argument("--request-id", help="retry this specific (for example failed) request")
    run.add_argument("--reload-serving", action="store_true", help="restart the inference service if a new champion was promoted")
    run.set_defaults(handler=run_controller)

    status = actions.add_parser("status", help="list recent retraining requests")
    status.add_argument("--limit", type=int, default=10)
    status.set_defaults(handler=show_requests)


def create_manual_request(args: argparse.Namespace) -> int:
    from churn_platform.config import load_dataset_params
    from churn_platform.retraining.requests import create_request
    from churn_platform.storage.db import connect

    settings = mlflow_settings()
    with connect(settings.ops_database_url) as conn:
        data_as_of = args.data_as_of or conn.execute(
            "SELECT max(snapshot_date) FROM ops.prediction_observations WHERE model_name = %s", (settings.model_name,)
        ).fetchone()[0] or load_dataset_params(settings.workspace).as_of
        request_id = create_request(conn, settings.model_name, "manual", [f"manual: {args.reason}"], data_as_of)
    if request_id is None:
        print("refused: an open retraining request already exists (see `churnctl retrain status`)")
        return 1
    print(f"created retraining request {request_id} (data as of {data_as_of})")
    return 0


def run_controller(args: argparse.Namespace) -> int:
    import uuid

    from churn_platform.config import load_retraining_config
    from churn_platform.retraining.controller import process_request
    from churn_platform.retraining.requests import claim_next

    settings = mlflow_settings()
    config = load_retraining_config(settings.config_dir)
    request_id = uuid.UUID(args.request_id) if args.request_id else None
    request = claim_next(settings.ops_database_url, settings.model_name, config.controller.stale_after_minutes, request_id)
    if request is None:
        print("no pending retraining request")
        return 0
    outcome = process_request(settings, registry_for(settings), request, config)
    print(json.dumps(outcome.__dict__, indent=2))
    if outcome.outcome == "promoted":
        if args.reload_serving:
            from churn_platform.cli.serving import reload_service

            return reload_service(argparse.Namespace(timeout=120))
        print("new champion promoted. Load it with: churnctl serving reload")
    return 1 if outcome.outcome == "failed" else 0


def show_requests(args: argparse.Namespace) -> int:
    from churn_platform.retraining.requests import list_requests

    settings = mlflow_settings()
    rows = list_requests(settings.ops_database_url, settings.model_name, args.limit)
    for row in rows:
        row["created_utc"] = row.pop("created_at").strftime("%Y-%m-%d %H:%M:%S")
        row["reasons"] = "; ".join(row["reasons"] or [])
        row["error"] = (row["error"] or "").splitlines()[0][:60] if row["error"] else ""
    print_table(rows, ["created_utc", "trigger", "status", "outcome", "data_as_of", "candidate_version",
                       "champion_before", "champion_after", "attempts", "reasons", "error"])
    return 0
