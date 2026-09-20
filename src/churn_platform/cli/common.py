"""Shared helpers for CLI commands: settings, MLflow/registry setup and small printing utilities."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from churn_platform.settings import Settings, load_settings


def mlflow_settings() -> Settings:
    from churn_platform.tracking.mlflow_setup import configure_mlflow

    settings = load_settings()
    configure_mlflow(settings)
    return settings


def registry_for(settings: Settings):
    from mlflow.tracking import MlflowClient

    from churn_platform.lifecycle.registry import ModelRegistry

    return ModelRegistry(MlflowClient(settings.mlflow_tracking_uri), settings.model_name)


def run_dvc(workspace: Path, *args: str) -> int:
    """Run DVC with this environment's interpreter first on PATH (DVC stages call `python`)."""
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.call([sys.executable, "-m", "dvc", *args], cwd=workspace, env=env)


def print_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))
