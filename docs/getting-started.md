# Getting started

> **In short:** Install Docker, uv and Git; run `uv sync` and `uv run churnctl bootstrap`;
> then train, approve and serve the first model with five commands. This guide explains
> every step, what it creates, how to check that it worked, and how to stop or remove
> everything again.

## Contents

1. [Prerequisites](#1-prerequisites)
2. [A short tour of the repository](#2-a-short-tour-of-the-repository)
3. [Install the Python environment](#3-install-the-python-environment)
4. [Start the platform](#4-start-the-platform)
5. [Verify the platform](#5-verify-the-platform)
6. [Train, approve and serve the first model](#6-train-approve-and-serve-the-first-model)
7. [Make a prediction](#7-make-a-prediction)
8. [Explore the user interfaces](#8-explore-the-user-interfaces)
9. [Run the tests](#9-run-the-tests)
10. [Stop, restart, reset and uninstall](#10-stop-restart-reset-and-uninstall)
11. [Environment variables and secrets](#11-environment-variables-and-secrets)
12. [Next steps](#12-next-steps)

---

## 1. Prerequisites

| Tool | Minimum version | Check with | Used for |
|---|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Docker Engine with the Compose plugin | Docker Compose **v2.20** | `docker compose version` | runs the nine platform containers; `--wait` (used by the setup commands) needs v2.20+ |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | **0.12** | `uv --version` | creates the Python 3.12 environment from the lock file; downloads Python 3.12 automatically if it is missing |
| Git | any recent version | `git --version` | DVC stores dataset versions next to Git history |
| curl (optional) | any | `curl --version` | the bash prediction example; PowerShell users use `Invoke-RestMethod` instead |

**Resources.** Allow Docker at least **6 GB of memory** (Docker Desktop -> Settings ->
Resources). The containers are limited to about 4.7 GB together. Plan for about **8 GB of
disk** for images, volumes and data.

**Ports.** The platform uses these ports on `127.0.0.1` only (nothing is reachable from
other machines). Stop other programs that use them:

| Port | Service |
|---|---|
| 3000 | Grafana |
| 5000 | MLflow |
| 5432 | PostgreSQL |
| 8000 | Inference API |
| 9000 | MinIO S3 API (used by DVC) |
| 9001 | MinIO web console |
| 9090 | Prometheus |

**Operating system.** Verified on Windows 11 with Docker Desktop. All commands are
platform-neutral and work in PowerShell, Command Prompt, bash and zsh; macOS and Linux
should work unchanged but were not tested.

---

## 2. A short tour of the repository

You do not need to know the code to run the platform, but it helps to know where things
are:

| Path | What it contains |
|---|---|
| `compose.yaml` | the definition of all containers: images, settings, health checks, ports |
| `config/` | every policy as YAML: data generation, training, quality gate, serving, monitoring, retraining |
| `params.yaml` | the one value that selects the dataset version (`dataset.as_of`) |
| `dvc.yaml` | the data and training pipeline (six stages) |
| `src/churn_platform/` | all Python code, including the `churnctl` command-line tool |
| `deploy/` | Dockerfiles and the setup of PostgreSQL, MinIO, Prometheus and Grafana |
| `tests/` | automated tests in five layers |
| `.env.example` | template for the secrets file `.env` (the real `.env` is created for you) |

`churnctl` is the platform's command-line tool. Every administrative action - starting the
stack, training, approving, rolling back, monitoring, retraining - is a `churnctl` command.
A full list is in [cli-reference.md](cli-reference.md).

---

## 3. Install the Python environment

From the repository root:

```bash
uv sync
```

What happens:

- uv reads `pyproject.toml` and `uv.lock`, downloads Python 3.12 if needed and creates a
  virtual environment in `.venv/`.
- It installs the platform package together with all optional parts (pipeline, serving,
  monitoring) and the test tools (pytest, ruff, pip-audit, boto3).

Every command in this documentation starts with `uv run`, which runs it inside that
environment. You never need to activate the environment manually (see
[section 10](#working-without-uv-run) if you prefer to).

Check it:

```bash
uv run churnctl --help
```

You should see the command groups `bootstrap, stack, pipeline, db, model, serving, traffic,
labels, monitor, retrain`.

---

## 4. Start the platform

```bash
uv run churnctl bootstrap
```

`bootstrap` performs three steps:

1. **Create secrets.** If `.env` does not exist, it copies `.env.example` and replaces every
   `change-me` with a random 48-character hexadecimal secret. An existing `.env` is never
   overwritten, because the database and storage volumes were initialised with its
   passwords.
2. **Build and start the containers.** It runs `docker compose up --detach --build --wait`.
   This builds three images (MLflow, inference API, monitoring worker), starts every
   service and waits until each health check passes. The first run takes a few minutes;
   later runs take seconds.
3. **Connect DVC to MinIO.** It writes the DVC storage credentials to `.dvc/config.local`
   (ignored by Git).

At the end it prints the addresses of the user interfaces.

### What gets created

| Kind | Name | Purpose |
|---|---|---|
| Long-running containers | `postgres`, `minio`, `mlflow`, `inference`, `monitoring-worker`, `prometheus`, `grafana` | the platform services |
| One-shot containers | `minio-init`, `db-migrate` | create storage buckets and users; create the operations database tables. They exit with code 0 when done |
| Docker volumes | `churn-platform_postgres-data`, `churn-platform_minio-data`, `churn-platform_prometheus-data`, `churn-platform_grafana-data` | all persistent state |
| PostgreSQL databases | `mlflow`, `churn_ops` | MLflow metadata; predictions, monitoring results, retraining requests, audit trail |
| MinIO buckets | `mlflow-artifacts`, `dvc-store` | model files and reports; dataset versions |
| Local files | `.env`, `.dvc/config.local` | secrets (never committed) |

---

## 5. Verify the platform

Run through this checklist once after the first start.

**All services are healthy:**

```bash
uv run churnctl stack status
```

Expected: `postgres`, `minio`, `mlflow`, `inference`, `prometheus` and `grafana` show
`Up ... (healthy)`; `monitoring-worker` shows `Up` (it has no health check); `minio-init` and
`db-migrate` show `Exited (0)`.

**The inference API is alive but not ready yet:**

```bash
curl http://127.0.0.1:8000/health
```

Expected: `{"status":"alive","model_loaded":false}`.

```bash
curl http://127.0.0.1:8000/ready
```

Expected: HTTP 503 with a reason such as `Registered Model with name=customer-churn-classifier
not found`. This is correct: no model has been approved yet, and the API refuses to pretend
otherwise.

PowerShell users can use `Invoke-RestMethod http://127.0.0.1:8000/health` instead of `curl`.

**The web interfaces open:** MLflow at http://127.0.0.1:5000 and Grafana at
http://127.0.0.1:3000 (see [section 8](#8-explore-the-user-interfaces)).

---

## 6. Train, approve and serve the first model

### 6.1 Build the dataset and train

```bash
uv run churnctl pipeline run
```

This runs the DVC pipeline (about one minute) and uploads the dataset version to MinIO.
The output ends like this:

```
logistic_regression: val F1=0.514 recall=0.712 ROC-AUC=0.856 threshold=0.60
random_forest: val F1=0.502 recall=0.566 ROC-AUC=0.845 threshold=0.54
hist_gradient_boosting: val F1=0.496 recall=0.528 ROC-AUC=0.839 threshold=0.67
cycle cycle-...: winner: logistic_regression
test F1=0.501 recall=0.674 ROC-AUC=0.848 (no-skill F1=0.232), p95 latency 8.1 ms
4 files pushed
```

Three models were trained and compared; the winner was evaluated once on held-out data.
What each stage does: [data-pipeline.md](data-pipeline.md).

### 6.2 Register the winner

```bash
uv run churnctl model register
```

Expected: `registered customer-churn-classifier version 1 (alias: candidate)`.

### 6.3 Run the quality gate

```bash
uv run churnctl model gate
```

The command prints a table with one row per check. Because there is no production model
yet, only the absolute checks apply. Expected last line:
`version 1 vs champion none: PASSED -> challenger`.

### 6.4 Promote it to production

```bash
uv run churnctl model promote
```

Expected: `version 1 is now champion. Restart serving to load it: churnctl serving reload`.

### 6.5 Load it into the API

```bash
uv run churnctl serving reload
```

Expected: `inference is ready and serving customer-churn-classifier version 1`.

> [!NOTE]
> If you skip this step, the API still picks up the first champion by itself within about
> 15 seconds, because it retries while it has no model. Later promotions always need a
> reload - a running service never swaps its model silently.

---

## 7. Make a prediction

The request contains a customer ID, the snapshot date and 16 customer facts.

**bash, zsh or Git Bash:**

```bash
curl -s -X POST http://127.0.0.1:8000/predict -H 'Content-Type: application/json' -d '{"customer_id": "C-1001", "snapshot_date": "2025-12-15", "features": {"plan_tier": "basic", "contract_type": "monthly", "autopay_enabled": false, "recent_price_increase": true, "tenure_months": 3, "monthly_charge": 24.5, "payment_failures_90d": 2, "late_payments_12m": 3, "monthly_usage_hours": 4.0, "sessions_30d": 3, "days_since_last_login": 21, "usage_trend_pct": -0.4, "support_tickets_90d": 2, "complaints_90d": 1, "avg_resolution_hours": 30.0, "features_adopted": 1}}'
```

**PowerShell:**

```powershell
$features = @{ plan_tier = "basic"; contract_type = "monthly"; autopay_enabled = $false; recent_price_increase = $true; tenure_months = 3; monthly_charge = 24.5; payment_failures_90d = 2; late_payments_12m = 3; monthly_usage_hours = 4.0; sessions_30d = 3; days_since_last_login = 21; usage_trend_pct = -0.4; support_tickets_90d = 2; complaints_90d = 1; avg_resolution_hours = 30.0; features_adopted = 1 }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/predict -ContentType "application/json" -Body (@{ customer_id = "C-1001"; snapshot_date = "2025-12-15"; features = $features } | ConvertTo-Json -Depth 3)
```

**Response:**

```json
{
  "prediction_id": "6d608adc-df94-4213-b641-6d13a2f87915",
  "customer_id": "C-1001",
  "snapshot_date": "2025-12-15",
  "churn_prediction": true,
  "churn_probability": 0.999552,
  "risk_level": "high",
  "decision_threshold": 0.6,
  "model_name": "customer-churn-classifier",
  "model_version": "1"
}
```

This new, unhappy customer (recent price increase, failed payments, falling usage,
complaints) is almost certain to churn. The response also says exactly which model
version answered.

Try an invalid request - for example `"plan_tier": "gold"` - and the API answers `422` with
a precise message; the model is never called. The browser page http://127.0.0.1:8000/docs
lets you send requests without a terminal. Field rules: [api-reference.md](api-reference.md).

---

## 8. Explore the user interfaces

| Interface | Address | Login | What to look at first |
|---|---|---|---|
| **MLflow** | http://127.0.0.1:5000 | none | *Experiments -> customer-churn*: the training cycle with three child runs; *Models -> customer-churn-classifier*: version 1 with alias `champion` |
| **Grafana** | http://127.0.0.1:3000 | none for viewing; admin user `admin`, password = `GRAFANA_ADMIN_PASSWORD` in `.env` | *Dashboards -> Churn Platform*: **Churn Inference Operations** and **Churn Model Monitoring** |
| **Prometheus** | http://127.0.0.1:9090 | none | *Status -> Targets* (the API should be `UP`) and *Alerts* |
| **MinIO console** | http://127.0.0.1:9001 | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` in `.env` | buckets `mlflow-artifacts` and `dvc-store` |
| **API documentation** | http://127.0.0.1:8000/docs | none | the `/predict` contract and a "Try it out" button |

The dashboards fill up once there is traffic; the [demo walkthrough](demo-walkthrough.md)
generates it.

---

## 9. Run the tests

The fast tests need nothing but the Python environment:

```bash
uv run pytest
```

This runs 172 unit and component tests in about 20 seconds. The slower layers use the
running platform:

| Command | Runs | Duration |
|---|---|---|
| `uv run pytest -m integration` | 21 tests against real MLflow, PostgreSQL, MinIO and DVC | about 7-8 minutes |
| `uv run pytest -m e2e` | 3 complete lifecycle workflows through the CLI and a real server | about 3-4 minutes |
| `uv run pytest -m operational` | 21 checks that restart and break real containers (needs a served champion) | about 4 minutes |

Integration and end-to-end tests use their own temporary copies and model names, so they
never change your demo model. Details: [testing-strategy.md](testing-strategy.md).

---

## 10. Stop, restart, reset and uninstall

| Goal | Command | Effect |
|---|---|---|
| Stop everything, keep all data | `uv run churnctl stack down` | containers removed, volumes kept |
| Start again | `uv run churnctl stack up` | containers recreated; models, runs and predictions are still there |
| Restart one service | `uv run churnctl stack restart inference` | restarts and waits until healthy |
| Delete all platform state | `uv run churnctl stack reset --yes` | removes containers, **volumes**, `data/`, the DVC cache and `.runtime/`; keeps `.env` |

After a reset, run `uv run churnctl bootstrap` again.

> [!WARNING]
> Keep `.env` as long as the volumes exist. If you delete `.env` while the volumes still
> exist, the newly generated passwords will not match the ones stored in PostgreSQL and
> MinIO. Fix: `uv run churnctl stack reset --yes`, then `uv run churnctl bootstrap`.

**Returning to the first dataset version.** Retraining moves `dataset.as_of` in
`params.yaml` forward. To repeat the demo from the beginning, set it back:

```yaml
dataset:
  as_of: "2026-01-01"
```

or, once the files are committed to Git, run `git checkout -- params.yaml dvc.lock reports/`.

**Uninstalling completely.** After `stack reset --yes`, remove the built images:

```bash
docker image rm churn-platform/inference:1.0.0 churn-platform/worker:1.0.0 churn-platform/mlflow:3.16.1
```

and delete the repository folder (including `.venv/` and `.env`).

### Working without `uv run`

You can activate the environment once instead of prefixing every command:

| Shell | Command |
|---|---|
| PowerShell | `.venv\Scripts\Activate.ps1` |
| Command Prompt | `.venv\Scripts\activate.bat` |
| bash / zsh | `source .venv/bin/activate` |

Then `churnctl` and `dvc` are available directly. Always run `dvc repro` from inside this
environment: the pipeline stages call `python` and must find the platform package.

---

## 11. Environment variables and secrets

You normally do not need to set anything: `bootstrap` creates `.env`, and the host-side
defaults point at the local stack. The variables are listed here so nothing is hidden.

**Secrets in `.env`** (generated, never committed):

| Variable | Used by |
|---|---|
| `POSTGRES_SUPERUSER_PASSWORD` | PostgreSQL superuser (only the first-start script uses it) |
| `MLFLOW_DB_PASSWORD`, `OPS_DB_PASSWORD`, `INFERENCE_DB_PASSWORD`, `GRAFANA_DB_PASSWORD` | the per-component database roles |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | MinIO administrator (only the setup container uses it) |
| `MLFLOW_S3_ACCESS_KEY`, `MLFLOW_S3_SECRET_KEY` | MLflow's storage key (limited to `mlflow-artifacts`) |
| `DVC_S3_ACCESS_KEY`, `DVC_S3_SECRET_KEY` | DVC's storage key (limited to `dvc-store`) |
| `GRAFANA_ADMIN_PASSWORD` | Grafana administrator |

**Optional overrides** (process environment or `.env`):

| Variable | Default | Purpose |
|---|---|---|
| `MLFLOW_TRACKING_URI` | `http://127.0.0.1:5000` | MLflow address |
| `OPS_DATABASE_URL` | built from `OPS_DB_PASSWORD` for `127.0.0.1:5432` | operations database |
| `CHURN_MODEL_NAME` | `customer-churn-classifier` | registered model name |
| `CHURN_EXPERIMENT_NAME` | `customer-churn` | MLflow experiment for training |
| `INFERENCE_URL` | `http://127.0.0.1:8000` | where traffic and `serving` commands send requests |

The complete list, including container-side values, is in
[configuration-reference.md](configuration-reference.md#environment-variables).

---

## 12. Next steps

- Run the full scenario - drift, delayed outcomes, retraining, rollback:
  [demo-walkthrough.md](demo-walkthrough.md)
- Understand what you just ran: [how-it-works.md](how-it-works.md)
- Everyday operations: [operations.md](operations.md)
- If anything did not work: [troubleshooting.md](troubleshooting.md)
