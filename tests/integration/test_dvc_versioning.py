"""DVC dataset versioning against the real MinIO remote.

Protects the data-lifecycle promises: a dataset version can be pushed to MinIO and pulled
back into an empty workspace byte-for-byte, a new version can be created by changing
parameters, and an older version can be restored from Git + DVC at any time.
"""

from __future__ import annotations

import pytest
import yaml

from churn_platform.pipeline.stages import file_md5
from tests.support.workspace import IsolatedWorkspace, dvc, git, remove_tree

pytestmark = pytest.mark.integration

RAW = "data/raw/snapshots.parquet"
DATA_STAGES = "split"  # `dvc repro split` runs generate -> validate -> prepare -> split only


def locked_md5(workspace, stage: str, path: str) -> str:
    lock = yaml.safe_load((workspace.root / "dvc.lock").read_text(encoding="utf-8"))
    return next(out["md5"] for out in lock["stages"][stage]["outs"] if out["path"] == path)


@pytest.fixture
def workspace(tmp_path):
    ws = IsolatedWorkspace(tmp_path / "ws").create()
    yield ws
    ws.delete_remote()


def test_dataset_round_trips_through_the_minio_remote(stack, workspace):
    dvc(workspace.root, "repro", DATA_STAGES)
    dvc(workspace.root, "push")
    original = (workspace.root / RAW).read_bytes()

    remove_tree(workspace.root / "data")
    remove_tree(workspace.root / ".dvc" / "cache")
    dvc(workspace.root, "pull")

    assert (workspace.root / RAW).read_bytes() == original
    assert workspace.remote_objects(), "expected objects under the test prefix in MinIO"


def test_previous_dataset_version_can_be_restored(stack, workspace):
    dvc(workspace.root, "repro", DATA_STAGES)
    dvc(workspace.root, "push")
    workspace.commit("dataset as of 2026-01-01")
    first_version = locked_md5(workspace, "generate", RAW)

    workspace.set_as_of("2026-07-01")
    dvc(workspace.root, "repro", DATA_STAGES)
    dvc(workspace.root, "push")
    workspace.commit("dataset as of 2026-07-01")
    assert locked_md5(workspace, "generate", RAW) != first_version

    git(workspace.root, "checkout", "-q", "HEAD~1")
    dvc(workspace.root, "checkout")

    status = dvc(workspace.root, "status", DATA_STAGES).stdout
    assert file_md5(workspace.root / RAW) == first_version
    assert "up to date" in status.lower() or status.strip() == ""
