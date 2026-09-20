#!/bin/sh
# One-shot MinIO setup, run by the `minio-init` container on every `docker compose up`.
# Every step is idempotent, so re-running it is safe.
#   mlflow-artifacts - MLflow model and report artifacts (written only by the MLflow server)
#   dvc-store        - DVC remote for dataset and pipeline outputs
# Each consumer gets its own access key restricted to its own bucket, so a leaked DVC key
# cannot overwrite model artifacts and vice versa. The root account is used only here.
set -eu

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null

for bucket in mlflow-artifacts dvc-store; do
  mc mb --ignore-existing "local/$bucket"
done

create_scoped_user() {
  user="$1"; secret="$2"; bucket="$3"
  policy="${bucket}-readwrite"
  cat > "/tmp/${policy}.json" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
     "Resource": ["arn:aws:s3:::${bucket}"]},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
     "Resource": ["arn:aws:s3:::${bucket}/*"]}
  ]
}
POLICY
  mc admin policy create local "$policy" "/tmp/${policy}.json" >/dev/null
  mc admin user add local "$user" "$secret" >/dev/null
  # Attaching an already attached policy returns an error; that is fine on re-runs.
  mc admin policy attach local "$policy" --user "$user" >/dev/null 2>&1 || true
  echo "user ${user} -> bucket ${bucket}"
}

create_scoped_user "$MLFLOW_S3_ACCESS_KEY" "$MLFLOW_S3_SECRET_KEY" mlflow-artifacts
create_scoped_user "$DVC_S3_ACCESS_KEY" "$DVC_S3_SECRET_KEY" dvc-store
echo "minio-init complete"
