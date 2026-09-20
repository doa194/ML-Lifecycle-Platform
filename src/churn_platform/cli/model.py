"""`churnctl model`: registry lifecycle operations (register, gate, promote, rollback, inspect).

These commands are the only way to change registry aliases. They run locally against
MLflow and are intentionally not exposed through the inference API.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC

from churn_platform.cli.common import mlflow_settings, print_table, registry_for


def register(groups: argparse._SubParsersAction) -> None:
    model = groups.add_parser("model", help="model registry lifecycle")
    actions = model.add_subparsers(dest="action", required=True)

    reg = actions.add_parser("register", help="register the last training winner as the candidate")
    reg.set_defaults(handler=register_candidate)

    gate = actions.add_parser("gate", help="run the quality gate on the candidate (or --version)")
    gate.add_argument("--version")
    gate.set_defaults(handler=run_gate)

    promote = actions.add_parser("promote", help="make the challenger (or --version) the champion")
    promote.add_argument("--version")
    promote.add_argument("--approved-by", help="approver name (required when manual approval is enabled)")
    promote.set_defaults(handler=promote_version)

    rollback = actions.add_parser("rollback", help="restore the previous champion (or --to-version)")
    rollback.add_argument("--to-version")
    rollback.add_argument("--reason", required=True, help="why the rollback happens (kept in the audit trail)")
    rollback.set_defaults(handler=rollback_champion)

    status = actions.add_parser("status", help="list versions with aliases, lifecycle status and key metrics")
    status.set_defaults(handler=show_status)

    history = actions.add_parser("history", help="show the lifecycle audit trail")
    history.add_argument("--limit", type=int, default=30)
    history.set_defaults(handler=show_history)


def _fail(error: Exception) -> int:
    print(f"refused: {error}")
    return 1


def register_candidate(args: argparse.Namespace) -> int:
    from churn_platform.lifecycle.registration import register_from_workspace
    from churn_platform.lifecycle.states import LifecycleError
    from churn_platform.pipeline.paths import PipelinePaths

    settings = mlflow_settings()
    try:
        result = register_from_workspace(registry_for(settings), PipelinePaths(settings.workspace), settings.ops_database_url)
    except LifecycleError as error:
        return _fail(error)
    verb = "registered" if result.created else "already registered as"
    print(f"{verb} {settings.model_name} version {result.version.version} (alias: candidate)")
    return 0


def run_gate(args: argparse.Namespace) -> int:
    from churn_platform.lifecycle.gate_runner import run_quality_gate
    from churn_platform.lifecycle.states import LifecycleError

    settings = mlflow_settings()
    try:
        decision = run_quality_gate(
            registry_for(settings), settings.workspace, settings.config_dir, settings.ops_database_url, args.version
        )
    except LifecycleError as error:
        return _fail(error)
    rows = [
        {"check": c.name, "result": "pass" if c.passed else "FAIL", "observed": c.observed, "threshold": c.threshold, "detail": c.detail}
        for c in decision.checks
    ]
    print_table(rows, ["check", "result", "observed", "threshold", "detail"])
    outcome = "PASSED -> challenger" if decision.passed else "FAILED -> rejected (champion unchanged)"
    print(f"\nversion {decision.candidate_version} vs champion {decision.champion_version or 'none'}: {outcome}")
    return 0 if decision.passed else 3


def promote_version(args: argparse.Namespace) -> int:
    from churn_platform.config import load_quality_gate_policy
    from churn_platform.lifecycle.promotion import promote
    from churn_platform.lifecycle.states import LifecycleError

    settings = mlflow_settings()
    try:
        state = promote(
            registry_for(settings), load_quality_gate_policy(settings.config_dir), settings.ops_database_url,
            args.version, args.approved_by,
        )
    except LifecycleError as error:
        return _fail(error)
    print(f"version {state.version} is now champion. Restart serving to load it: churnctl serving reload")
    return 0


def rollback_champion(args: argparse.Namespace) -> int:
    from churn_platform.lifecycle.promotion import rollback
    from churn_platform.lifecycle.states import LifecycleError

    settings = mlflow_settings()
    try:
        state = rollback(registry_for(settings), settings.ops_database_url, args.reason, args.to_version)
    except LifecycleError as error:
        return _fail(error)
    print(f"champion rolled back to version {state.version}. Restart serving to load it: churnctl serving reload")
    return 0


def show_status(args: argparse.Namespace) -> int:
    settings = mlflow_settings()
    registry = registry_for(settings)
    rows = []
    for state in registry.all_versions():
        metrics = registry.client.get_run(registry.run_id(state.version)).data.metrics
        rows.append(
            {
                "version": state.version,
                "aliases": ",".join(sorted(state.aliases)) or "-",
                "status": state.status or "-",
                "gate": state.tags.get("gate.status", "-"),
                "algorithm": state.tags.get("training.algorithm", "-"),
                "data_as_of": state.tags.get("data.as_of", "-"),
                "test_f1": f"{metrics.get('test_f1', float('nan')):.3f}",
                "test_recall": f"{metrics.get('test_recall', float('nan')):.3f}",
                "test_roc_auc": f"{metrics.get('test_roc_auc', float('nan')):.3f}",
            }
        )
    print(f"registered model: {settings.model_name}")
    print_table(rows, ["version", "aliases", "status", "gate", "algorithm", "data_as_of", "test_f1", "test_recall", "test_roc_auc"])
    return 0


def show_history(args: argparse.Namespace) -> int:
    from churn_platform.lifecycle.audit import recent_events

    settings = mlflow_settings()
    events = recent_events(settings.ops_database_url, settings.model_name, args.limit)
    rows = [
        {"time_utc": e["occurred_at"].astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"), "version": e["version"], "event": e["event_type"],
         "actor": e["actor"], "details": json.dumps(e["details"], default=str)}
        for e in events
    ]
    print_table(rows, ["time_utc", "version", "event", "actor", "details"])
    return 0
