# Configuration reference

> **In short:** Policies (thresholds, windows, algorithms) live in versioned YAML files in
> `config/` plus `params.yaml`; machine-specific values and secrets come from environment
> variables and the `.env` file. Every YAML file is loaded into a strict model
> (`churn_platform/config.py`): unknown keys, missing keys and out-of-range values are
> rejected when the file is loaded, long before they could affect a decision.

## Contents

- [Where configuration lives](#where-configuration-lives)
- [When changes take effect](#when-changes-take-effect)
- [params.yaml](#paramsyaml)
- [config/data.yaml](#configdatayaml)
- [config/training.yaml](#configtrainingyaml)
- [config/quality_gate.yaml](#configquality_gateyaml)
- [config/serving.yaml](#configservingyaml)
- [config/monitoring.yaml](#configmonitoringyaml)
- [config/retraining.yaml](#configretrainingyaml)
- [Environment variables](#environment-variables)
- [Secrets in .env](#secrets-in-env)
- [Validation examples](#validation-examples)

---

## Where configuration lives

| File | Controls | Read by |
|---|---|---|
| `params.yaml` | which dataset version to build (`dataset.as_of`) | pipeline; changed by the retraining controller |
| `config/data.yaml` | data generation, profiles, timeline, split, validation | pipeline, traffic simulator, outcome resolution |
| `config/training.yaml` | algorithms, threshold search, segments, reference size | pipeline, quality gate |
| `config/quality_gate.yaml` | promotion checks and approval | gate, promotion, retraining controller |
| `config/serving.yaml` | risk levels, persistence policy, model-load retry, body limit | inference service |
| `config/monitoring.yaml` | windows, drift methods and thresholds, worker interval | monitoring worker and CLI |
| `config/retraining.yaml` | retraining policy and controller behaviour | monitoring (policy), retraining controller |
| `.env` | secrets and optional overrides | CLI, Docker Compose |
| `compose.yaml` | container settings and container-side environment variables | Docker Compose |

---

## When changes take effect

| Change | Takes effect |
|---|---|
| `params.yaml`, `config/data.yaml`, `config/training.yaml` | on the next `churnctl pipeline run` (DVC re-runs the affected stages) |
| `config/quality_gate.yaml` | on the next gate, promotion or retraining command |
| `config/monitoring.yaml`, `config/retraining.yaml` | on the next monitoring cycle or command (the worker re-reads them every cycle); only `worker.interval_seconds` needs `uv run churnctl stack restart monitoring-worker` |
| `config/serving.yaml` | after `uv run churnctl stack restart inference` (read once at startup) |
| `.env` values used by containers | after `uv run churnctl stack up` (containers are recreated); database and storage passwords can only be changed with a reset |

Containers mount `config/` read-only, so the files on your disk are what they read.

---

## params.yaml

| Key | Default | Meaning |
|---|---|---|
| `dataset.as_of` | `"2026-01-01"` | date of the (simulated) data-warehouse extract; the dataset contains the 365 days of snapshots whose 30-day outcome is known on this date. The retraining controller moves it forward |

---

## config/data.yaml

### `generation`

| Key | Default | Meaning |
|---|---|---|
| `seed` | 20260101 | random seed of the extract |
| `rows` | 12000 | number of customer snapshots |
| `window_days` | 365 | days the snapshots are spread over |
| `label_window_days` | 30 | churn outcome horizon |

### `timeline`

A list of `{start, profile}` entries in date order. Default: `baseline` from 2000-01-01,
`pricing_shift` from 2026-01-01. Every profile named here must exist.

### `profiles`

Named parameter sets. `baseline` defines everything; other profiles use `extends: baseline`
and override single values (nested values such as `churn_model` are merged key by key).

| Key | `baseline` | Meaning |
|---|---|---|
| `plan_mix` | basic 0.45, standard 0.35, premium 0.20 | plan shares (must sum to 1) |
| `contract_mix` | monthly 0.55, annual 0.30, two_year 0.15 | contract shares (must sum to 1) |
| `base_charge` | 19 / 39 / 69 | monthly base price per plan |
| `price_increase_rate` | 0.10 | share of customers with a recent price increase |
| `price_increase_pct` | [0.04, 0.10] | size range of the increase |
| `autopay_rate` | 0.62 | share with autopay (+0.12 on annual/two-year contracts) |
| `payment_failure_rate` | 0.15 | average payment failures per 90 days (higher without autopay) |
| `usage_hours_median` | 22 | typical monthly usage |
| `usage_trend_mean` / `usage_trend_sd` | 0.0 / 0.15 | usage trend distribution |
| `login_gap_scale` | 1.0 | multiplier for days between logins |
| `support_ticket_rate` | 0.6 | average tickets per 90 days |
| `complaint_share` | 0.25 | share of tickets that are complaints |
| `resolution_hours_median` | 18 | typical resolution time |
| `feature_adoption_rate` | 0.45 | adoption probability per feature |
| `churn_model` | see [synthetic-world.md](synthetic-world.md#the-hidden-churn-logic) | coefficients of the hidden churn formula |

### `split`

| Key | Default | Meaning |
|---|---|---|
| `validation_days` | 90 | validation window before the test period (its last 30 days are purged) |
| `test_days` | 60 | newest days = test period |
| `purge_days` | 30 | gap removed before each boundary; **must be >= `label_window_days`** |
| `min_rows_per_split` | 500 | a smaller period fails the split stage |

### `validation`

| Key | Default | Meaning |
|---|---|---|
| `min_rows` | 5000 | minimum extract size |
| `churn_rate_min` / `churn_rate_max` | 0.05 / 0.45 | plausible churn-rate band |

---

## config/training.yaml

| Key | Default | Meaning |
|---|---|---|
| `random_state` | 42 | seed for every estimator |
| `selection_metric` | `f1` | winner metric: `f1`, `roc_auc` or `recall` |
| `threshold_search.min` / `max` / `step` | 0.05 / 0.90 / 0.01 | decision-threshold grid (min must be below max) |
| `new_customer_months` | 6 | "new customer" boundary for the derived feature and the segment |
| `algorithms.<name>` | three algorithms | hyperparameters; allowed names `logistic_regression`, `random_forest`, `hist_gradient_boosting` (at least one) |
| `evaluation.segment_columns` | plan_tier, contract_type, tenure_segment | segments to report |
| `evaluation.min_segment_rows` | 50 | smaller segments are not "reliable" |
| `evaluation.latency_samples` | 200 | single-row predictions timed |
| `reference.max_rows` | 5000 | size of the monitoring reference |

---

## config/quality_gate.yaml

| Key | Default | Meaning |
|---|---|---|
| `absolute.f1_min` | 0.40 | minimum F1 |
| `absolute.recall_min` | 0.50 | minimum recall |
| `absolute.roc_auc_min` | 0.75 | minimum ROC-AUC |
| `segments.columns` | plan_tier, contract_type, tenure_segment | segments the gate enforces |
| `segments.recall_min` | 0.25 | minimum recall in every reliable segment |
| `segments.max_f1_drop_vs_champion` | 0.08 | largest allowed F1 loss in any shared segment |
| `champion_comparison.min_f1_delta` | -0.01 | candidate F1 - champion F1 must be at least this |
| `champion_comparison.min_roc_auc_delta` | -0.01 | the same for ROC-AUC |
| `operational.max_p95_latency_ms` | 50 | single-prediction p95 limit |
| `approval.manual_approval_required` | false | require `--approved-by` for promotion |

---

## config/serving.yaml

| Key | Default | Meaning |
|---|---|---|
| `risk_levels.medium_ratio` | 0.6 | `medium` risk starts at this fraction of the model's threshold |
| `observations.on_persistence_failure` | `reject` | `reject` (503 when a prediction cannot be stored) or `serve` |
| `model_loading.retry_interval_seconds` | 15 | retry interval while no model is loaded; 0 disables retrying |
| `max_request_bytes` | 16384 | larger bodies get 413 |

---

## config/monitoring.yaml

| Key | Default | Meaning |
|---|---|---|
| `window.max_observations` | 1000 | current window size |
| `window.min_observations` | 200 | minimum for drift statistics |
| `drift.numerical_method` | `wasserstein` | distance for numeric features (fixed) |
| `drift.categorical_method` | `jensenshannon` | distance for categorical/boolean features (fixed) |
| `drift.feature_threshold` | 0.1 | per-feature drift threshold |
| `drift.dataset_drift_share` | 0.25 | share of drifted features for dataset drift |
| `drift.prediction_threshold` | 0.1 | prediction drift threshold |
| `data_quality.max_missing_share_increase` | 0.10 | allowed rise in missing values of required features |
| `data_quality.max_duplicate_share` | 0.05 | allowed duplicate snapshots |
| `performance.min_labeled` | 200 | outcomes needed for accuracy |
| `performance.max_labeled` | 2000 | newest outcomes used |
| `worker.interval_seconds` | 300 | worker cycle interval |

---

## config/retraining.yaml

| Key | Default | Meaning |
|---|---|---|
| `policy.min_observations` | 500 | minimum window before any decision |
| `policy.severe_drift_share` | 0.25 | drift share that can trigger retraining |
| `policy.require_prediction_drift` | true | severe drift must also move predictions |
| `policy.max_f1_drop` / `policy.max_recall_drop` | 0.05 / 0.10 | accuracy drops that trigger retraining |
| `policy.block_on_data_quality_issues` | true | never retrain on flagged data |
| `policy.cooldown_hours` | 12 | minimum time between requests |
| `controller.auto_promote` | true | promote a passing candidate automatically |
| `controller.stale_after_minutes` | 60 | a `running` request older than this may be re-claimed |
| `controller.push_data` | true | `dvc push` after the pipeline |

---

## Environment variables

`churn_platform/settings.py` reads each value in this order: the process environment, then
`.env` in the workspace, then the default.

| Variable | Default on the host | In containers | Purpose |
|---|---|---|---|
| `MLFLOW_TRACKING_URI` | `http://127.0.0.1:5000` | `http://mlflow:5000` | MLflow address |
| `OPS_DATABASE_URL` | built from `OPS_DB_PASSWORD`, `POSTGRES_HOST` (`127.0.0.1`) and `POSTGRES_PORT` (`5432`), role `churn_ops` | inference: role `churn_inference`; worker: role `churn_ops` | operations database |
| `CHURN_MODEL_NAME` | `customer-churn-classifier` | same | registered model name (tests use unique names) |
| `CHURN_EXPERIMENT_NAME` | `customer-churn` | - | training experiment; monitoring uses `<name>-monitoring` |
| `INFERENCE_URL` | `http://127.0.0.1:8000` | - | used by `traffic send` and `serving` commands |
| `MINIO_ENDPOINT` | `http://127.0.0.1:9000` | - | used by tests that inspect buckets |
| `CHURN_WORKSPACE` | the nearest folder containing `dvc.yaml` | `/app` | where `params.yaml`, `config/` and `data/` are |
| `CHURN_CONFIG_DIR` | `<workspace>/config` | `/app/config` | policy files |

**MLflow client settings** applied by `churn_platform/tracking/mlflow_setup.py` (as defaults;
an existing environment value wins):

| Variable | Value | Why |
|---|---|---|
| `MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD` / `..._UPLOAD` | `false` | all artifact traffic goes through MLflow's proxy; direct MinIO links would point at an address the host cannot reach |
| `MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT` | `true` | keeps command output clean and machine-readable |
| `MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR` | `false` | the same |
| `MLFLOW_DISABLE_AGENT_HINT` | `1` | suppresses an informational message printed on import |

**Set in `compose.yaml` for the inference service:** `MLFLOW_HTTP_REQUEST_MAX_RETRIES=1`
and `MLFLOW_HTTP_REQUEST_TIMEOUT=20`, so a registry outage fails fast and the service's own
retry loop takes over. The worker image sets `DO_NOT_TRACK=1` and
`EVIDENTLY_DISABLE_TELEMETRY=1`.

---

## Secrets in .env

`uv run churnctl bootstrap` creates `.env` from `.env.example`, replacing every `change-me`
with a random 48-character hexadecimal value. `.env` is ignored by Git; never commit it.

| Variable | Used by |
|---|---|
| `COMPOSE_PROJECT_NAME` | Docker Compose project name (`churn-platform`) |
| `POSTGRES_SUPERUSER_PASSWORD` | PostgreSQL superuser; used only by the first-start script |
| `MLFLOW_DB_PASSWORD` | role `mlflow` |
| `OPS_DB_PASSWORD` | role `churn_ops` (CLI, monitoring worker, schema migrations) |
| `INFERENCE_DB_PASSWORD` | role `churn_inference` (insert predictions only) |
| `GRAFANA_DB_PASSWORD` | role `grafana_reader` (read-only) |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | MinIO administrator; used only by `minio-init` and the MinIO console |
| `MLFLOW_S3_ACCESS_KEY` / `MLFLOW_S3_SECRET_KEY` | MLflow server; access to bucket `mlflow-artifacts` only |
| `DVC_S3_ACCESS_KEY` / `DVC_S3_SECRET_KEY` | DVC on the host; access to bucket `dvc-store` only (copied to `.dvc/config.local`) |
| `GRAFANA_ADMIN_PASSWORD` | Grafana `admin` user |

> [!WARNING]
> The database and storage volumes are initialised with these passwords on first start.
> Changing or deleting `.env` afterwards breaks the logins. To change them, reset the platform
> (`uv run churnctl stack reset --yes`) and bootstrap again.

---

## Validation examples

Every loader rejects invalid configuration with a message that names the problem. Examples
covered by unit tests (`tests/unit/test_config.py`):

| Change | Error |
|---|---|
| `split.purge_days: 10` (shorter than the 30-day label window) | `split.purge_days must be >= generation.label_window_days` |
| a timeline entry referring to a profile that does not exist | `timeline references unknown profiles: ['unknown']` |
| a plan mix that does not sum to 1 | `probabilities must sum to 1` |
| an unknown key such as `generation.unexpected_key` | `Extra inputs are not permitted` |
