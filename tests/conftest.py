"""Shared pytest fixtures.

Unit and component tests need nothing outside the Python process. Tests marked
`integration`, `e2e` or `operational` need the local stack (`churnctl bootstrap`); they fail
fast with a clear message instead of hanging when it is not running.
"""

from __future__ import annotations

import os
import uuid

import pytest

from churn_platform.settings import load_settings
from churn_platform.tracking.mlflow_setup import configure_mlflow
from tests.support.stack import mlflow_is_up, postgres_is_up

os.environ.setdefault("PYTHONUTF8", "1")


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture(scope="session")
def stack(settings):
    """Require the running stack and configure the MLflow client for it."""
    missing = [
        name
        for name, up in (("MLflow", mlflow_is_up(settings)), ("PostgreSQL", postgres_is_up(settings)))
        if not up
    ]
    if missing:
        pytest.fail(f"{', '.join(missing)} not reachable - start the stack with `churnctl bootstrap`")
    configure_mlflow(settings)
    return settings


@pytest.fixture
def unique_name():
    """A short unique prefix so tests never collide with demo state or with each other."""
    return f"test-{uuid.uuid4().hex[:10]}"
