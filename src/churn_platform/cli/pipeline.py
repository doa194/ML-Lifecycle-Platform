"""`churnctl pipeline` and `churnctl db`: run the DVC pipeline and manage the ops database schema."""

from __future__ import annotations

import argparse

from churn_platform.cli.common import run_dvc
from churn_platform.settings import find_workspace, load_settings


def register(groups: argparse._SubParsersAction) -> None:
    pipeline = groups.add_parser("pipeline", help="run the DVC data + training pipeline")
    actions = pipeline.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run", help="dvc repro, then dvc push the new data version to MinIO")
    run.add_argument("--no-push", action="store_true", help="do not push data to the DVC remote")
    run.add_argument("--force", action="store_true", help="re-run every stage even if nothing changed")
    run.set_defaults(handler=run_pipeline)
    status = actions.add_parser("status", help="show which stages are out of date")
    status.set_defaults(handler=lambda a: run_dvc(find_workspace(), "status"))

    db = groups.add_parser("db", help="operations database (PostgreSQL) schema")
    db_actions = db.add_subparsers(dest="action", required=True)
    migrate = db_actions.add_parser("migrate", help="apply pending schema migrations (idempotent)")
    migrate.set_defaults(handler=migrate_db)


def run_pipeline(args: argparse.Namespace) -> int:
    workspace = find_workspace()
    code = run_dvc(workspace, "repro", *(["--force"] if args.force else []))
    if code != 0 or args.no_push:
        return code
    return run_dvc(workspace, "push")


def migrate_db(args: argparse.Namespace) -> int:
    from churn_platform.storage.db import migrate

    applied = migrate(load_settings().ops_database_url)
    print(f"applied: {', '.join(applied)}" if applied else "schema is up to date")
    return 0
