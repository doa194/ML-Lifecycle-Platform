# CLI reference: `churnctl`

> **In short:** `churnctl` is the platform's command-line tool. Every administrative action -
> starting the stack, building data, training, approving, promoting, rolling back,
> monitoring, retraining, simulating traffic - is a `churnctl` command. Keeping these
> actions on the command line (instead of HTTP endpoints) means the public prediction API can
> never be used to change models.

## Contents

- [Usage](#usage)
- [Platform: bootstrap, stack, db](#platform-bootstrap-stack-db)
- [Data and training: pipeline](#data-and-training-pipeline)
- [Model lifecycle: model](#model-lifecycle-model)
- [Serving: serving](#serving-serving)
- [Monitoring: monitor](#monitoring-monitor)
- [Continuous training: retrain](#continuous-training-retrain)
- [Simulators: traffic, labels](#simulators-traffic-labels)
- [Using DVC directly](#using-dvc-directly)

---

## Usage

```bash
uv run churnctl <group> <command> [options]
```

| Global option | Effect |
|---|---|
| `-h`, `--help` | help for `churnctl`, a group or a command (e.g. `uv run churnctl model promote --help`) |
| `-v`, `--verbose` | debug logging |

Run commands from the repository root. They read service addresses and secrets from `.env`
and the environment ([configuration-reference.md](configuration-reference.md#environment-variables)).
Exit code 0 means success; a non-zero code means the action was refused or failed, with the
reason printed.

---

## Platform: bootstrap, stack, db

### `bootstrap`

```bash
uv run churnctl bootstrap
```

Creates `.env` with random secrets if it does not exist, runs
`docker compose up --detach --build --wait`, and writes the DVC storage credentials to
`.dvc/config.local`. Prints the addresses of all user interfaces. Safe to run again.

### `stack`

| Command | Does |
|---|---|
| `stack up [SERVICE...]` | build and start (all or the named) services and wait until they are healthy |
| `stack down` | stop and remove the containers; **volumes (all data) are kept** |
| `stack status` | list all containers with state, health and ports (`docker compose ps --all`) |
| `stack restart SERVICE...` | restart the named services and wait until healthy |
| `stack reset --yes` | **delete everything**: containers, volumes (models, runs, predictions, dataset remote), `data/`, the DVC cache and `.runtime/`; `.env` is kept. Without `--yes` it refuses |

Examples:

```bash
uv run churnctl stack restart inference monitoring-worker
```

```bash
uv run churnctl stack reset --yes
```

### `db`

| Command | Does |
|---|---|
| `db migrate` | apply pending SQL migrations of the operations database; prints `schema is up to date` when there is nothing to do (the `db-migrate` container runs it on every `stack up`) |

---

## Data and training: pipeline

| Command | Does |
|---|---|
| `pipeline run` | `dvc repro` (runs every stage whose inputs changed), then `dvc push` |
| `pipeline run --no-push` | the same without uploading to MinIO |
| `pipeline run --force` | re-run every stage even if nothing changed |
| `pipeline status` | `dvc status`: which stages are out of date |

Output of `pipeline run` includes each algorithm's validation scores, the winner and its test
scores. Details: [data-pipeline.md](data-pipeline.md).

---

## Model lifecycle: model

| Command | Does | Exit codes |
|---|---|---|
| `model register` | register the winner of the last pipeline run as a new version with alias `candidate` (returns the existing version if already registered) | 0 ok; 1 refused (incomplete or untraceable run) |
| `model gate [--version N]` | run the quality gate for the candidate (or version N) against the champion; prints every check | 0 passed; 3 failed; 1 refused |
| `model promote [--version N] [--approved-by NAME]` | make the challenger (or version N) the champion | 0 ok; 1 refused, listing every reason |
| `model rollback --reason TEXT [--to-version N]` | restore the previous (or given) validated champion | 0 ok; 1 refused |
| `model status` | table of all versions: aliases, status, gate result, algorithm, dataset date, test F1/recall/ROC-AUC | 0 |
| `model history [--limit N]` | the lifecycle audit trail, newest first (default 30 rows, UTC times) | 0 |

Example output of `model status`:

```
registered model: customer-churn-classifier
version  aliases             status    gate    algorithm            data_as_of  test_f1  test_recall  test_roc_auc
1        -                   retired   passed  logistic_regression  2026-01-01  0.501    0.674        0.848
2        candidate,champion  champion  passed  logistic_regression  2026-12-31  0.700    0.700        0.851
```

Details: [model-lifecycle.md](model-lifecycle.md), [quality-gate.md](quality-gate.md).

---

## Serving: serving

| Command | Does |
|---|---|
| `serving reload [--timeout SECONDS]` | restart the inference container and wait (default 120 s) until `/ready` succeeds; prints the served version, or the not-ready reason and exit code 1 |
| `serving status` | readiness and the served version (exit code 1 when not ready) |

```
inference is ready and serving customer-churn-classifier version 2
```

---

## Monitoring: monitor

| Command | Does |
|---|---|
| `monitor run` | run one monitoring cycle now (always analyses) and print a JSON summary |
| `monitor run --loop [--interval SECONDS]` | worker mode: repeat forever, skip cycles without new data (used by the container) |
| `monitor status [--limit N]` | the latest monitoring results (default 10) |

The JSON summary contains `model_version`, `observations`, `dataset_drift`, `drift_share`,
`drifted_features`, `prediction_drift`, `data_quality_ok`, `labeled`,
`production_performance`, `f1_drop_vs_test`, `decision`, `reasons` and
`retraining_request_id`. Details: [ml-monitoring.md](ml-monitoring.md).

---

## Continuous training: retrain

| Command | Does |
|---|---|
| `retrain request --reason TEXT [--data-as-of YYYY-MM-DD]` | create a manual retraining request; refused if one is already open. Default date: newest production snapshot, else `params.yaml` |
| `retrain run` | claim and process the next pending request; prints the outcome as JSON |
| `retrain run --request-id ID` | retry a specific (for example failed) request |
| `retrain run --reload-serving` | also restart the inference service if a new champion was promoted |
| `retrain status [--limit N]` | recent requests with status, outcome, versions and reasons |

When there is nothing to do, `retrain run` prints `no pending retraining request` and exits
with 0. It exits with 1 when the request failed. Details:
[continuous-training.md](continuous-training.md).

---

## Simulators: traffic, labels

These commands stand in for the outside world - customer applications calling the API and
the billing system reporting cancellations.

### `traffic send`

```bash
uv run churnctl traffic send --start 2026-01-01 --end 2026-12-31 --count 1200
```

| Option | Default | Meaning |
|---|---|---|
| `--start`, `--end` | required | range of simulated snapshot dates (`YYYY-MM-DD`) |
| `--count` | 500 | number of customers |
| `--profile` | from the timeline | force a profile: `baseline`, `pricing_shift`, `engagement_shift` |
| `--seed` | derived from dates and profile | the same arguments replay the same customers; set a seed for new ones |
| `--invalid-share` | 0 | share of deliberately invalid requests (rejected with 422) |
| `--concurrency` | 4 | parallel requests |

Prints a JSON summary: status counts, profiles used, predicted churn share and client-side p95
latency. Exit code 1 if no request succeeded (for example the API is not ready).

### `labels resolve`

```bash
uv run churnctl labels resolve --as-of 2026-06-30
```

Reveals the true outcome of every prediction whose 30-day window closed by the given
(simulated) date. Details: [delayed-ground-truth.md](delayed-ground-truth.md).

---

## Using DVC directly

DVC commands work as usual when run inside the project environment:

| Command | Does |
|---|---|
| `uv run dvc repro` | run the pipeline (without pushing) |
| `uv run dvc repro split` | run only the data stages up to `split` |
| `uv run dvc push` / `uv run dvc pull` | upload / download dataset files |
| `uv run dvc status` | stages whose inputs changed |
| `uv run dvc checkout` | restore data files matching `dvc.lock` |
