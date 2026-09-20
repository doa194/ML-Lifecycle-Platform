"""The DVC pipeline stages: generate -> validate -> prepare -> split -> train -> evaluate.

Each stage reads its inputs from files, writes its outputs to files and returns an exit
code. DVC decides *when* a stage runs (only when its declared inputs changed); these
functions only decide *what* it does. Keeping stages as plain file-in/file-out functions
means an orchestrator such as Kubeflow could call the same code later without changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from churn_platform.config import load_data_config, load_dataset_params
from churn_platform.data.generator import generate_dataset, snapshot_window
from churn_platform.data.preparation import prepare_dataset
from churn_platform.data.schema import FEATURE_SCHEMA_VERSION
from churn_platform.data.splitting import SplitError, time_split
from churn_platform.data.validation import validate_dataset
from churn_platform.pipeline.paths import PipelinePaths
from churn_platform.settings import find_workspace

log = logging.getLogger(__name__)
DATASET_NAME = "customer-snapshots"


def file_md5(path: Path) -> str:
    """Content hash in the same form DVC records in dvc.lock for single files."""
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _context():
    workspace = find_workspace()
    return workspace, PipelinePaths(workspace), load_data_config(workspace / "config")


def generate() -> int:
    workspace, paths, config = _context()
    as_of = load_dataset_params(workspace).as_of
    frame = generate_dataset(config, as_of)
    write_parquet(frame, paths.raw)
    first, last = snapshot_window(as_of, config)
    profiles = {config.profile_for(d.date()) for d in frame["snapshot_date"]}
    # Human-readable identity of this dataset version (the DVC hash is the exact identity).
    write_json(
        paths.dataset_report,
        {
            "dataset": DATASET_NAME,
            "as_of": str(as_of),
            "seed": config.generation.seed,
            "rows": len(frame),
            "first_snapshot": str(first),
            "last_snapshot": str(last),
            "profiles": sorted(profiles),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "raw_md5": file_md5(paths.raw),
        },
    )
    log.info("generated %d snapshots (%s .. %s) as of %s", len(frame), first, last, as_of)
    return 0


def validate() -> int:
    workspace, paths, config = _context()
    as_of = load_dataset_params(workspace).as_of
    report = validate_dataset(pd.read_parquet(paths.raw), config, as_of)
    write_json(paths.validation_report, report.to_dict())
    for failure in report.failures:
        log.error("validation failed: %s - %s", failure.name, failure.detail)
    if not report.passed:
        return 1
    log.info("all %d validation checks passed", len(report.checks))
    return 0


def prepare() -> int:
    _, paths, _ = _context()
    prepared = prepare_dataset(pd.read_parquet(paths.raw))
    write_parquet(prepared, paths.prepared)
    log.info("prepared %d rows", len(prepared))
    return 0


def split() -> int:
    _, paths, config = _context()
    try:
        result = time_split(pd.read_parquet(paths.prepared), config.split)
    except SplitError as error:
        log.error("%s", error)
        return 1
    for name in ("train", "validation", "test"):
        write_parquet(getattr(result, name), paths.split(name))
    write_json(paths.split_report, result.report())
    log.info("split: %s", {k: v for k, v in result.report().items() if k in ("train", "validation", "test")})
    return 0


def train() -> int:
    from churn_platform.training.cycle import run_training_stage

    return run_training_stage()


def evaluate() -> int:
    from churn_platform.training.evaluation import run_evaluation_stage

    return run_evaluation_stage()


STAGES: dict[str, Callable[[], int]] = {
    "generate": generate,
    "validate": validate,
    "prepare": prepare,
    "split": split,
    "train": train,
    "evaluate": evaluate,
}
