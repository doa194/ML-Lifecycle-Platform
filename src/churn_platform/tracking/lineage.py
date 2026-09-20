"""Lineage facts recorded on every training run: which data and which code produced a model.

With these tags any registered model version can be traced back:
    model version -> MLflow run -> dataset hashes (DVC) + as-of date -> Git commit + config
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from churn_platform.config import load_dataset_params
from churn_platform.data.schema import FEATURE_SCHEMA_VERSION
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.pipeline.stages import DATASET_NAME, file_md5

# Tags every candidate run must carry before it may be registered (see registry checks).
REQUIRED_LINEAGE_TAGS = (
    "data.id",
    "data.as_of",
    "data.train_md5",
    "data.validation_md5",
    "data.test_md5",
    "code.git_commit",
    "feature_schema_version",
    "training.cycle_id",
    "training.algorithm",
    "model.fingerprint",
)


def git_revision(workspace: Path) -> dict[str, str]:
    """Current commit and whether the working tree has uncommitted changes."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=workspace, capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()

    try:
        commit = git("rev-parse", "HEAD")
    except (subprocess.SubprocessError, FileNotFoundError):
        # A repository without commits (or without git) still gets an explicit value.
        return {"code.git_commit": "unavailable", "code.git_dirty": "unknown"}
    try:
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except subprocess.SubprocessError:
        dirty = True
    return {"code.git_commit": commit, "code.git_dirty": str(dirty).lower()}


def dataset_lineage(workspace: Path) -> dict[str, str]:
    """Content hashes of the split files (identical to DVC's hashes) plus the as-of date."""
    paths = PipelinePaths(workspace)
    as_of = load_dataset_params(workspace).as_of
    hashes = {name: file_md5(paths.split(name)) for name in ("train", "validation", "test")}
    return {
        "data.id": f"{DATASET_NAME}@{as_of}#{hashes['train'][:8]}{hashes['test'][:8]}",
        "data.as_of": str(as_of),
        "data.train_md5": hashes["train"],
        "data.validation_md5": hashes["validation"],
        "data.test_md5": hashes["test"],
    }


def base_lineage_tags(workspace: Path) -> dict[str, str]:
    return {
        **dataset_lineage(workspace),
        **git_revision(workspace),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
    }
