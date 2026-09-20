"""Isolated copies of the project workspace for tests that run DVC or the full lifecycle.

The copy has its own Git repository, its own DVC cache and its own prefix in the MinIO DVC
bucket, so tests can commit, change parameters and push data without touching the
developer's working copy or the demo dataset.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

from churn_platform.cli.stack import remove_tree  # noqa: F401 - re-exported for tests
from churn_platform.settings import read_dotenv
from tests.support.data import REPO_ROOT

VENV_BIN = Path(sys.executable).parent
INITIAL_AS_OF = "2026-01-01"  # every snapshot is before the simulated pricing shift


def tool_env(workspace: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    # DVC stages call `python`; make sure that is the project interpreter.
    env["PATH"] = f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    env["CHURN_WORKSPACE"] = str(workspace)
    env["PYTHONUTF8"] = "1"
    env.update(extra or {})
    return env


def run(workspace: Path, *command: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command), cwd=workspace, env=tool_env(workspace, extra_env), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"{' '.join(command)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result


def dvc(workspace: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return run(workspace, sys.executable, "-m", "dvc", *args, extra_env=extra_env)


def git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(workspace, "git", *args)


class IsolatedWorkspace:
    def __init__(self, root: Path):
        self.root = root
        self.remote_prefix = f"tests/{uuid.uuid4().hex[:12]}"
        self.env = read_dotenv(REPO_ROOT / ".env")

    def create(self) -> IsolatedWorkspace:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("dvc.yaml", "params.yaml", ".env", ".gitignore"):
            shutil.copy2(REPO_ROOT / name, self.root / name)
        shutil.copytree(REPO_ROOT / "config", self.root / "config")
        # Start from a known dataset version, whatever the developer's params.yaml says now.
        self.set_as_of(INITIAL_AS_OF)
        # Stage dependencies are hashed from these paths; the code that actually runs is the
        # installed package, which points at the same source.
        shutil.copytree(REPO_ROOT / "src", self.root / "src", ignore=shutil.ignore_patterns("__pycache__"))
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "churn-platform tests")
        dvc(self.root, "init", "-q")
        dvc(self.root, "config", "core.analytics", "false")
        dvc(self.root, "remote", "add", "-d", "minio", f"s3://dvc-store/{self.remote_prefix}")
        dvc(self.root, "remote", "modify", "minio", "endpointurl", "http://127.0.0.1:9000")
        dvc(self.root, "remote", "modify", "minio", "region", "us-east-1")
        dvc(self.root, "remote", "modify", "--local", "minio", "access_key_id", self.env["DVC_S3_ACCESS_KEY"])
        dvc(self.root, "remote", "modify", "--local", "minio", "secret_access_key", self.env["DVC_S3_SECRET_KEY"])
        self.commit("initial workspace")
        return self

    def commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)

    def set_as_of(self, as_of: str) -> None:
        path = self.root / "params.yaml"
        text = path.read_text(encoding="utf-8")
        lines = [f'  as_of: "{as_of}"' if line.strip().startswith("as_of:") else line for line in text.splitlines()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def remote_objects(self) -> list[str]:
        response = self._s3().list_objects_v2(Bucket="dvc-store", Prefix=self.remote_prefix)
        return [obj["Key"] for obj in response.get("Contents", [])]

    def delete_remote(self) -> None:
        s3 = self._s3()
        for key in self.remote_objects():
            s3.delete_object(Bucket="dvc-store", Key=key)

    def _s3(self):
        return boto3.client(
            "s3",
            endpoint_url="http://127.0.0.1:9000",
            aws_access_key_id=self.env["DVC_S3_ACCESS_KEY"],
            aws_secret_access_key=self.env["DVC_S3_SECRET_KEY"],
            region_name="us-east-1",
            config=Config(signature_version="s3v4"),
        )
