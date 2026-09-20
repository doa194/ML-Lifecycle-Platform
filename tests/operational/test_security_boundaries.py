"""Security boundaries of the running platform: least-privilege storage credentials,
non-root and read-only containers, and no administrative operations on the public API.
"""

from __future__ import annotations

import boto3
import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from churn_platform.settings import read_dotenv
from tests.support.data import REPO_ROOT
from tests.support.stack import compose

pytestmark = pytest.mark.operational

PUBLIC_API = {"/predict", "/health", "/ready", "/model"}


def s3_client(access_key: str, secret_key: str):
    return boto3.client(
        "s3", endpoint_url="http://127.0.0.1:9000", aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        region_name="us-east-1", config=Config(signature_version="s3v4"),
    )


@pytest.mark.parametrize(
    ("key", "secret", "own_bucket", "foreign_bucket"),
    [
        ("MLFLOW_S3_ACCESS_KEY", "MLFLOW_S3_SECRET_KEY", "mlflow-artifacts", "dvc-store"),
        ("DVC_S3_ACCESS_KEY", "DVC_S3_SECRET_KEY", "dvc-store", "mlflow-artifacts"),
    ],
)
def test_each_storage_key_is_limited_to_its_own_bucket(stack, key, secret, own_bucket, foreign_bucket):
    env = read_dotenv(REPO_ROOT / ".env")
    client = s3_client(env[key], env[secret])

    client.list_objects_v2(Bucket=own_bucket, MaxKeys=1)
    with pytest.raises(ClientError, match="AccessDenied"):
        client.list_objects_v2(Bucket=foreign_bucket, MaxKeys=1)
    with pytest.raises(ClientError, match="AccessDenied"):
        client.put_object(Bucket=foreign_bucket, Key="intrusion-test.txt", Body=b"x")


@pytest.mark.parametrize("service", ["inference", "monitoring-worker", "mlflow", "grafana", "prometheus"])
def test_services_run_as_non_root_users(stack, service):
    uid = compose("exec", "-T", service, "id", "-u").stdout.strip()

    assert uid and uid != "0"


@pytest.mark.parametrize("service", ["inference", "monitoring-worker"])
def test_platform_containers_have_a_read_only_filesystem(stack, service):
    result = compose("exec", "-T", service, "python", "-c", "open('/app/tampered.txt', 'w')", check=False)

    assert result.returncode != 0 and "Read-only file system" in result.stderr


def test_public_api_exposes_no_lifecycle_operations(live_inference):
    paths = set(httpx.get(f"{live_inference}/openapi.json", timeout=10).json()["paths"])

    assert paths == PUBLIC_API
    for method, path in (("POST", "/promote"), ("POST", "/model"), ("POST", "/rollback"), ("POST", "/retrain")):
        assert httpx.request(method, f"{live_inference}{path}", timeout=10).status_code in (404, 405)
