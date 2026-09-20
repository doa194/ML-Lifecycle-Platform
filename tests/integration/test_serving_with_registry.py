"""Inference service against the real registry and PostgreSQL.

Protects what component tests with fakes cannot: the service resolves and verifies the real
`champion` alias from MLflow, and every prediction lands in PostgreSQL tagged with the
version that produced it. Also checks the database role boundaries the service relies on.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from churn_platform.config import load_serving_config
from churn_platform.lifecycle.states import Alias
from churn_platform.serving.app import create_app
from churn_platform.settings import read_dotenv
from churn_platform.storage.db import connect
from tests.support.data import REPO_ROOT
from tests.support.serving import valid_payload

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(champion_sandbox):
    settings = champion_sandbox.settings()
    app = create_app(settings=settings, serving_config=load_serving_config(settings.config_dir))
    with TestClient(app) as test_client:
        yield test_client


def test_service_loads_the_verified_registry_champion(client, champion_sandbox):
    registry = champion_sandbox.registry()
    champion = registry.alias_state(Alias.CHAMPION)

    lineage = client.get("/model").json()

    assert client.get("/ready").status_code == 200
    assert lineage["model_version"] == champion.version == champion_sandbox.champion_version
    assert lineage["fingerprint_sha256"] == champion.tags["model.fingerprint"]
    assert lineage["dataset"]["train_md5"] == champion.tags["data.train_md5"]


def test_prediction_is_persisted_with_the_serving_version(client, champion_sandbox):
    response = client.post("/predict", json=valid_payload())
    body = response.json()

    with connect(champion_sandbox.settings().ops_database_url) as conn:
        row = conn.execute(
            "SELECT model_name, model_version, churn_probability, features, actual_churn "
            "FROM ops.prediction_observations WHERE prediction_id = %s",
            (body["prediction_id"],),
        ).fetchone()

    assert response.status_code == 200
    assert row[0] == champion_sandbox.model_name and row[1] == body["model_version"]
    assert row[2] == pytest.approx(body["churn_probability"], abs=1e-6)
    assert row[3] == valid_payload()["features"]
    assert row[4] is None  # the outcome is unknown at prediction time


def test_inference_role_may_insert_but_not_read_observations(stack):
    password = read_dotenv(REPO_ROOT / ".env")["INFERENCE_DB_PASSWORD"]
    url = f"postgresql://churn_inference:{password}@127.0.0.1:5432/churn_ops"

    with psycopg.connect(url) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT count(*) FROM ops.prediction_observations")
    with psycopg.connect(url) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM ops.lifecycle_events")
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(url.replace("/churn_ops", "/mlflow"), connect_timeout=3).close()
