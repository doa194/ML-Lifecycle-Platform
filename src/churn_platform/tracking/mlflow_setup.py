"""One place where every component (pipeline, lifecycle CLI, serving, monitoring) configures
its MLflow client, so they all talk to the tracking server the same way.
"""

from __future__ import annotations

import os

from churn_platform.settings import Settings

# The MLflow server proxies all artifact traffic to MinIO. Since MLflow 3.16, clients
# otherwise ask the server for presigned MinIO URLs, which point at the internal
# `minio:9000` address and fail from the host. Forcing proxied transfers also keeps the
# security boundary simple: only the MLflow server ever talks to the model artifact bucket.
_CLIENT_DEFAULTS = {
    "MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD": "false",
    "MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD": "false",
    # Keep command output readable (and machine-parsable): no run links or progress bars.
    "MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT": "true",
    "MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR": "false",
    "MLFLOW_DISABLE_AGENT_HINT": "1",
}


def configure_mlflow(settings: Settings) -> None:
    for name, value in _CLIENT_DEFAULTS.items():
        os.environ.setdefault(name, value)
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_registry_uri(settings.mlflow_tracking_uri)
