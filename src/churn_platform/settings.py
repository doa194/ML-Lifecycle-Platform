"""Runtime settings that come from the environment: service addresses, credentials and names.

Policy values (thresholds, windows, algorithms) live in `config/*.yaml` and are loaded by
`churn_platform.config`. This module only holds values that change between machines or
contain secrets, so nothing secret ever has to be committed to the repository.

Lookup order for every value: real environment variable -> `.env` file in the workspace
-> a default that works for the host CLI talking to the local Docker Compose stack.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

DEFAULT_MODEL_NAME = "customer-churn-classifier"
DEFAULT_EXPERIMENT_NAME = "customer-churn"


def find_workspace(start: Path | None = None) -> Path:
    """Return the project root: CHURN_WORKSPACE if set, else the nearest folder with dvc.yaml."""
    explicit = os.environ.get("CHURN_WORKSPACE")
    if explicit:
        return Path(explicit).resolve()
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "dvc.yaml").exists() or (candidate / "config" / "data.yaml").exists():
            return candidate
    return current


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines. Comments and blank lines are ignored."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    workspace: Path
    mlflow_tracking_uri: str
    ops_database_url: str
    model_name: str
    experiment_name: str
    inference_url: str
    minio_endpoint: str

    @property
    def config_dir(self) -> Path:
        return Path(os.environ.get("CHURN_CONFIG_DIR", self.workspace / "config"))


def load_settings(workspace: Path | None = None) -> Settings:
    root = workspace or find_workspace()
    dotenv = read_dotenv(root / ".env")

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name) or dotenv.get(name) or default

    postgres_host = get("POSTGRES_HOST", "127.0.0.1")
    postgres_port = get("POSTGRES_PORT", "5432")
    # The host CLI connects as the ops-database owner. Containers pass a complete URL
    # instead (for example the inference service uses its insert-only role).
    ops_url = get("OPS_DATABASE_URL") or (
        f"postgresql://churn_ops:{quote(get('OPS_DB_PASSWORD'), safe='')}"
        f"@{postgres_host}:{postgres_port}/churn_ops"
    )
    return Settings(
        workspace=root,
        mlflow_tracking_uri=get("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"),
        ops_database_url=ops_url,
        model_name=get("CHURN_MODEL_NAME", DEFAULT_MODEL_NAME),
        experiment_name=get("CHURN_EXPERIMENT_NAME", DEFAULT_EXPERIMENT_NAME),
        inference_url=get("INFERENCE_URL", "http://127.0.0.1:8000"),
        minio_endpoint=get("MINIO_ENDPOINT", "http://127.0.0.1:9000"),
    )
