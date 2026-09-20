#!/bin/sh
# Runs once, when the PostgreSQL data volume is first initialised.
# Creates one database per concern on the single local server:
#   mlflow     - MLflow tracking and registry metadata (owned by role `mlflow`)
#   churn_ops  - platform operations data: prediction observations, monitoring results,
#                retraining requests, lifecycle audit events (owned by role `churn_ops`)
# Extra roles follow least privilege: `churn_inference` may only insert predictions and
# `grafana_reader` may only read ops tables. Their table grants are applied by the ops
# schema migrations (`churnctl db migrate`), because the tables do not exist yet here.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v mlflow_pw="$MLFLOW_DB_PASSWORD" \
  -v ops_pw="$OPS_DB_PASSWORD" \
  -v inference_pw="$INFERENCE_DB_PASSWORD" \
  -v grafana_pw="$GRAFANA_DB_PASSWORD" <<'EOSQL'
CREATE ROLE mlflow LOGIN PASSWORD :'mlflow_pw';
CREATE DATABASE mlflow OWNER mlflow;

CREATE ROLE churn_ops LOGIN PASSWORD :'ops_pw';
CREATE DATABASE churn_ops OWNER churn_ops;

CREATE ROLE churn_inference LOGIN PASSWORD :'inference_pw';
CREATE ROLE grafana_reader LOGIN PASSWORD :'grafana_pw';

-- Nobody else may connect to the component databases.
REVOKE ALL ON DATABASE mlflow FROM PUBLIC;
REVOKE ALL ON DATABASE churn_ops FROM PUBLIC;
GRANT CONNECT ON DATABASE churn_ops TO churn_inference, grafana_reader;
EOSQL
