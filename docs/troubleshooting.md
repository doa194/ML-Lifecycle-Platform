# Troubleshooting

> **In short:** Symptom -> likely cause -> fix, grouped by area. Start with
> `uv run churnctl stack status` and `uv run churnctl serving status`; they answer most
> questions. For the platform's behaviour during failures see
> [failure-recovery.md](failure-recovery.md).

## Contents

- [First checks](#first-checks)
- [Setup and startup](#setup-and-startup)
- [Pipeline and DVC](#pipeline-and-dvc)
- [Registry and lifecycle](#registry-and-lifecycle)
- [Serving](#serving)
- [Monitoring and retraining](#monitoring-and-retraining)
- [Windows specifics](#windows-specifics)
- [MLflow scripts of your own](#mlflow-scripts-of-your-own)

---

## First checks

| Check | Command | Healthy result |
|---|---|---|
| containers | `uv run churnctl stack status` | long-running services `healthy` (worker `Up`), `minio-init` and `db-migrate` `Exited (0)` |
| serving | `uv run churnctl serving status` | `ready: True` with a version |
| models | `uv run churnctl model status` | one version with alias `champion` |
| logs of a service | `docker compose logs --tail 100 <service>` | no repeated errors |

---

## Setup and startup

| Symptom | Likely cause | Fix |
|---|---|---|
| `required variable ... is missing a value: run churnctl bootstrap to create .env` | Docker Compose was started without `.env` | run `uv run churnctl bootstrap` |
| `password authentication failed`, or MLflow never becomes healthy after `.env` was deleted or edited | the volumes were initialised with the old passwords | `uv run churnctl stack reset --yes`, then `uv run churnctl bootstrap` |
| `port is already allocated` / `address already in use` | another program uses 3000, 5000, 5432, 8000, 9000, 9001 or 9090 | stop that program (the ports are fixed in `compose.yaml`) |
| `unknown flag: --wait` | Docker Compose older than v2.20 | update Docker Desktop / the Compose plugin |
| containers are killed or restart repeatedly | not enough memory for Docker | give Docker at least 6 GB (Docker Desktop -> Settings -> Resources) |
| the first `bootstrap` takes several minutes | images are being built | normal; later starts take seconds |
| `churnctl: command not found` | the command was run outside the project environment | prefix with `uv run`, or activate `.venv` |

---

## Pipeline and DVC

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: churn_platform` during `dvc repro`, or a wrong Python version | DVC ran outside the project environment; stages call `python` | use `uv run churnctl pipeline run` or `uv run dvc repro` |
| `dvc push` fails with `Access Denied` or missing credentials | `.dvc/config.local` is missing | `uv run churnctl bootstrap` writes it again |
| the `validate` stage fails | the data broke a validation rule | open `reports/data_validation.json`; each failed check has a detail message |
| `split periods below min_rows_per_split` | a data window too small for the split settings | increase `generation.rows` or reduce the split periods |
| `train` fails with a connection error | MLflow is not running | `uv run churnctl stack up` |
| `Data and pipelines are up to date` but you expected training | nothing that `train` depends on changed | change a setting, or `uv run churnctl pipeline run --force` |

---

## Registry and lifecycle

| Symptom | Likely cause | Fix |
|---|---|---|
| `register` refuses: missing lineage tags or metrics | the run did not come from a complete `train` + `evaluate` of this pipeline (for example `evaluate` failed) | run `uv run churnctl pipeline run` again |
| `register` refuses: "evaluation report does not belong to the training winner" | `reports/` is out of date | run `uv run churnctl pipeline run` |
| gate refuses: "local test split does not match version N's lineage" | the workspace holds a different dataset version than the candidate (for example after retraining moved `params.yaml`) | restore the matching `params.yaml`/`dvc.lock` and run `uv run dvc checkout`, or gate the version that belongs to the current data |
| gate refuses: "transition champion -> challenger is not allowed" | you tried to gate a current or former champion | gate a newer candidate instead |
| gate stops: "current champion vN cannot be evaluated" | the champion's model file is damaged | roll back to a healthy version first |
| `promote` refuses: "quality gate compared against champion 'X', current champion is 'Y'" | the champion changed after the gate ran | `uv run churnctl model gate --version N`, then promote |
| `promote` refuses: "manual approval is required" | `approval.manual_approval_required` is on | add `--approved-by <name>` |
| `rollback` refuses: "no previous validated champion" | only one version was ever champion | nothing to roll back to |

---

## Serving

| Symptom (`/ready` reason) | Likely cause | Fix |
|---|---|---|
| `Registered Model with name=... not found` or `alias champion not found` | nothing has been promoted yet | promote a model; the API picks it up within 15 s or after `uv run churnctl serving reload` |
| `Max retries exceeded` / connection errors | MLflow is not reachable | `uv run churnctl stack up`; the API recovers by itself |
| `ModelIntegrityError: ... fingerprint ... expected ...` | the model file in storage differs from the trained one | roll back and reload; investigate who changed the `mlflow-artifacts` bucket |
| `observation store unavailable` | PostgreSQL is not reachable from the API | `uv run churnctl stack up`; `docker compose logs postgres` |

| Other symptom | Likely cause | Fix |
|---|---|---|
| the API serves an old version after promotion | expected: the service loads the champion only at startup | `uv run churnctl serving reload` |
| `422` responses | the request breaks the contract | the response body names the field; common causes: `3.5` for an integer, `"true"` instead of `true`, `avg_resolution_hours` given with 0 tickets (or missing with tickets), complaints above tickets, an unknown field ([api-reference.md](api-reference.md#fields)) |
| `503 prediction could not be recorded` | PostgreSQL write failed in `reject` mode | restore PostgreSQL; or set `observations.on_persistence_failure: serve` if availability matters more |
| `413 request body too large` | body above 16 KB | send one customer per request |

---

## Monitoring and retraining

| Symptom | Likely cause | Fix |
|---|---|---|
| `monitor run` reports `insufficient_data` | fewer than 200 predictions of the served version (drift) or fewer than 500 (policy) | send more traffic |
| no new monitoring results although the worker runs | the worker skips cycles when no prediction or outcome was added | `uv run churnctl monitor run` always analyses |
| decision `blocked` with "an open retraining request already exists" | a request is pending or running | `uv run churnctl retrain run` |
| decision `blocked` with "cooldown active until ..." | a request was created less than 12 hours ago | wait, or create a manual request (`retrain request`) |
| decision `blocked` with "data quality issues" | duplicates or missing values in the window | fix the data source |
| production F1 is **higher** than test F1 even though drift was detected | F1 rises when churn becomes more common | compare ROC-AUC instead ([ml-monitoring.md](ml-monitoring.md#reading-the-results-correctly)) |
| `retrain run` prints `no pending retraining request` | nothing to do | normal |
| retraining outcome `rejected` | the new model did not beat the champion | read the gate report on the new version's MLflow run |
| retraining outcome `unchanged` | the data did not change since the champion was trained | normal |

---

## Windows specifics

| Symptom | Cause | Fix |
|---|---|---|
| `UnicodeEncodeError: 'charmap' codec can't encode character` from direct `python`/`dvc` calls | the console uses a legacy code page and some libraries print symbols | `churnctl` handles this itself; for direct calls set `PYTHONUTF8=1` (PowerShell: `$env:PYTHONUTF8 = "1"`) |
| deleting `.dvc/cache` by hand fails with "Access is denied" | DVC marks cache files read-only | use `uv run churnctl stack reset --yes`, which clears the flag, or remove the read-only attribute first |
| Git Bash turns `/bin/sh` into `C:/Program Files/Git/usr/bin/sh` in `docker run` commands | Git Bash path conversion | prefix the command with `MSYS_NO_PATHCONV=1` |
| `curl` in Windows PowerShell 5 behaves differently | `curl` is an alias of `Invoke-WebRequest` there | use `Invoke-RestMethod` (see [api-reference.md](api-reference.md#examples-in-powershell-and-bash)) or `curl.exe` |

---

## MLflow scripts of your own

If you write your own MLflow scripts against this platform and artifact downloads **hang**:
MLflow 3.16 clients ask the server for direct (presigned) MinIO links by default. Those links
point at the container-internal address `minio:9000`, which the host cannot reach. The
platform's own code disables this in `churn_platform/tracking/mlflow_setup.py`. In your
script, either call the same setup first:

```python
from churn_platform.settings import load_settings
from churn_platform.tracking.mlflow_setup import configure_mlflow

configure_mlflow(load_settings())
```

or set the environment variable `MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD=false`.
