"""MLflow tracking storage: metadata lands in PostgreSQL and artifacts in MinIO.

These protect the Phase-1 storage contract that every later lifecycle step depends on:
runs written through the tracking API can be read back by an independent client, and
artifacts are physically stored in the MinIO bucket (not on some container's disk).
"""

from __future__ import annotations

import boto3
import mlflow
import pytest
from botocore.config import Config
from mlflow.tracking import MlflowClient

from churn_platform.settings import read_dotenv

pytestmark = pytest.mark.integration


def test_run_metadata_is_readable_by_an_independent_client(stack, unique_name):
    mlflow.set_experiment(unique_name)
    with mlflow.start_run() as run:
        mlflow.log_params({"algorithm": "logistic_regression", "C": 0.5})
        mlflow.log_metric("f1", 0.61)
        mlflow.set_tag("data.as_of", "2026-01-01")

    stored = MlflowClient(stack.mlflow_tracking_uri).get_run(run.info.run_id)

    assert stored.data.params == {"algorithm": "logistic_regression", "C": "0.5"}
    assert stored.data.metrics["f1"] == pytest.approx(0.61)
    assert stored.data.tags["data.as_of"] == "2026-01-01"
    assert stored.info.status == "FINISHED"


def test_artifacts_are_stored_in_the_minio_bucket(stack, unique_name, tmp_path):
    source = tmp_path / "report.json"
    source.write_text('{"ok": true}', encoding="utf-8")
    mlflow.set_experiment(unique_name)
    with mlflow.start_run() as run:
        mlflow.log_artifact(str(source), artifact_path="reports")

    downloaded = mlflow.artifacts.download_artifacts(
        run_id=run.info.run_id, artifact_path="reports/report.json", dst_path=str(tmp_path / "dl")
    )
    env = read_dotenv(stack.workspace / ".env")
    s3 = boto3.client(
        "s3",
        endpoint_url=stack.minio_endpoint,
        aws_access_key_id=env["MLFLOW_S3_ACCESS_KEY"],
        aws_secret_access_key=env["MLFLOW_S3_SECRET_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )
    prefix = f"{run.info.experiment_id}/{run.info.run_id}/artifacts/"
    objects = s3.list_objects_v2(Bucket="mlflow-artifacts", Prefix=prefix).get("Contents", [])

    assert open(downloaded, encoding="utf-8").read() == '{"ok": true}'
    assert [obj["Key"] for obj in objects] == [f"{prefix}reports/report.json"]
