# Design decisions

> **In short:** This document records the important choices behind the platform. Each
> decision follows the same structure: the problem it addresses, what was chosen, which
> alternatives were considered, what the choice gives us, and what it costs. Read the
> summary table first, then any decision you are curious about.

## Summary

| # | Decision | One-line reason |
|---|---|---|
| 1 | [DVC pipelines as the orchestrator](#1-dvc-pipelines-as-the-orchestrator) | reproducible, cached pipelines and data versions without running a scheduler |
| 2 | [MLflow aliases are the only source of truth for model roles](#2-mlflow-aliases-are-the-only-source-of-truth-for-model-roles) | one authoritative answer to "which model is in production?" |
| 3 | [Artifacts only through the MLflow proxy](#3-artifacts-only-through-the-mlflow-proxy) | one component holds the model-storage key |
| 4 | [Safe model format plus fingerprint](#4-safe-model-format-plus-fingerprint) | a changed or malicious model file cannot be loaded |
| 5 | [One model object with the threshold inside](#5-one-model-object-with-the-threshold-inside) | production computes exactly what training evaluated |
| 6 | [Time-aware split with purging](#6-time-aware-split-with-purging) | honest evaluation for a forecasting problem |
| 7 | [A simulated world with a timeline and hidden outcomes](#7-a-simulated-world-with-a-timeline-and-hidden-outcomes) | reproducible drift, delayed feedback and retraining |
| 8 | [Monitor each version against its own reference](#8-monitor-each-version-against-its-own-reference) | meaningful prediction drift, clean start after promotion |
| 9 | [Fixed drift methods](#9-fixed-drift-methods) | "drifted" means the same thing for every window size |
| 10 | [A retraining policy with severity, cooldown and one open request](#10-a-retraining-policy-with-severity-cooldown-and-one-open-request) | retrain on real evidence, never in loops |
| 11 | [Load once, retry only until loaded, reload explicitly](#11-load-once-retry-only-until-loaded-reload-explicitly) | predictable serving without hot-swap risks |
| 12 | [Every served prediction is recorded](#12-every-served-prediction-is-recorded) | complete data for monitoring and accuracy measurement |
| 13 | [Lifecycle operations are local commands](#13-lifecycle-operations-are-local-commands) | the public API cannot change models |
| 14 | [One PostgreSQL server, separate databases and roles](#14-one-postgresql-server-separate-databases-and-roles) | separation and least privilege without extra servers |
| 15 | [Segments are compared on the primary metric](#15-segments-are-compared-on-the-primary-metric) | do not reject a better model for a precision/recall trade |
| 16 | [Production accuracy uses the training metric code](#16-production-accuracy-uses-the-training-metric-code) | production and test numbers are directly comparable |
| 17 | [The monitoring worker skips unchanged data](#17-the-monitoring-worker-skips-unchanged-data) | no gigabytes of identical reports |
| 18 | [Deterministic randomness everywhere](#18-deterministic-randomness-everywhere) | identical results on every run and machine |

---

## 1. DVC pipelines as the orchestrator

**Context.** Data preparation and training must be reproducible, and re-running everything
after every small change wastes time. Dataset files are too large for Git.

**Decision.** A `dvc.yaml` with six file-in/file-out stages (`generate`, `validate`,
`prepare`, `split`, `train`, `evaluate`). DVC re-runs a stage only when its declared code,
parameters or input files changed, records every output's content hash in `dvc.lock`, and
stores the files in a MinIO remote.

**Alternatives considered.**

| Alternative | Why not |
|---|---|
| Airflow | needs a scheduler, a metadata database and workers to operate; built for many scheduled jobs, not one local pipeline |
| Kubeflow Pipelines | needs Kubernetes; far too heavy for one machine |
| Plain Python scripts | no caching of unchanged stages, no data versioning |

**Benefits.** No server to run; stage-level caching; data versions tied to Git commits;
`dvc repro` explains exactly which stage re-runs and why.

**Trade-offs and limits.** No scheduling, retries or distributed execution. To keep a future
move to an orchestrator cheap, each stage is a plain function in
`churn_platform/pipeline/stages.py`; wrapping them as Kubeflow or Airflow tasks would not
change their logic.

---

## 2. MLflow aliases are the only source of truth for model roles

**Context.** The platform needs to know, reliably, which model version is the candidate, the
challenger and the champion.

**Decision.** These roles are MLflow Model Registry **aliases**. Version tags add history
(status, gate result, timestamps). A separate audit table (`ops.lifecycle_events`) records
who did what, but no code ever reads it to make a decision.

**Alternatives considered.**

| Alternative | Why not |
|---|---|
| A custom "deployments" table | duplicates the registry; two stores eventually disagree |
| MLflow's older "stages" (Staging/Production) | deprecated in favour of aliases; fixed names only |

**Benefits.** One answer to "what is in production?"; promotion and rollback are atomic
pointer changes, instant and without copying files.

**Trade-offs and limits.** The audit event is written *after* the registry change. If
PostgreSQL is unavailable at exactly that moment, the change stands and the missing event is
logged as an error - the registry stays correct, the history has a gap.

---

## 3. Artifacts only through the MLflow proxy

**Context.** Model files live in MinIO. If every component accessed MinIO directly, every
component would need the storage key for models.

**Decision.** The MLflow server runs with `--serve-artifacts` and MinIO as the destination;
all uploads and downloads go through MLflow's HTTP API. The shared MLflow client setup
(`churn_platform/tracking/mlflow_setup.py`) also turns off MLflow 3.16's presigned-URL
transfers, which would otherwise hand clients direct MinIO links.

**Alternatives considered.** Direct S3 access from every client (more keys to protect; the
host would also have to resolve the container-internal `minio` hostname).

**Benefits.** Only the MLflow server holds the model bucket's key; clients need nothing but
the MLflow address.

**Trade-offs and limits.** All artifact traffic passes through one server - fine for models
of a few hundred kilobytes and reports of a few megabytes, not for very large artifacts.

---

## 4. Safe model format plus fingerprint

**Context.** Python's default model format (pickle) can execute arbitrary code when a file
is loaded, and a model file could be corrupted or replaced after it was tested.

**Decision.** Models are saved in the **skops** format with an explicit list of trusted
object types (`TRUSTED_MODEL_TYPES`). A **SHA-256 fingerprint** of all model files is
recorded right after training and verified on every load - by evaluation, the gate,
promotion, rollback and serving.

**Alternatives considered.**

| Alternative | Why not |
|---|---|
| pickle / cloudpickle | executes code on load; MLflow 3.16 refuses it by default anyway |
| Cryptographically signed artifacts | needs key management; more than a local platform requires |

**Benefits.** A tampered, truncated or swapped model is refused everywhere with a clear
error; loading a model cannot run unexpected code.

**Trade-offs and limits.** Adding an algorithm with new internal types means extending the
trusted list (a unit test fails until that is done). The fingerprint orders files by their
plain path string: operating systems compare file names differently (Windows ignores upper
and lower case, Linux does not), and a model trained on a Windows host must verify inside
the Linux containers.

---

## 5. One model object with the threshold inside

**Context.** If production computes features even slightly differently from training
(*training-serving skew*), predictions silently degrade.

**Decision.** Feature engineering (`FeatureEngineer`), preprocessing (`ColumnTransformer`) and
the classifier form a single scikit-learn `Pipeline`, saved as one MLflow model. The
F1-optimal decision threshold is stored in the model's metadata and used by every consumer.

**Alternatives considered.** Feature code inside the API (two implementations to keep in
sync); a fixed 0.5 threshold (poor when only ~13% of customers churn).

**Benefits.** The API, the quality gate and the monitoring reference use exactly the
evaluated object and threshold; a unit test proves that a request sent through the API
conversion scores identically to the same training row.

**Trade-offs and limits.** The custom `FeatureEngineer` class must be importable wherever a
model is loaded; it ships in the same Python package as the loaders.

---

## 6. Time-aware split with purging

**Context.** Predicting churn is forecasting. Evaluating on customers observed *before* the
training customers overstates quality.

**Decision.** The newest 60 days are the test period, the 90 days before them the
validation period, the rest the training period. Rows whose 30-day outcome window would
overlap the next period are removed.

**Alternatives considered.** A random split (leaks the future); a time split without purging
(training labels would describe the validation period).

**Benefits.** Scores reflect how the model would perform on the next period of real data.

**Trade-offs and limits.** About 16% of rows are purged (1,906 of 12,000 in the first
dataset). See [leakage-controls.md](leakage-controls.md).

---

## 7. A simulated world with a timeline and hidden outcomes

**Context.** The platform must show drift, delayed outcomes and retraining - reproducibly,
without private data.

**Decision.** One generator produces both the training extracts and the production traffic.
A timeline in `config/data.yaml` switches the world from `baseline` to `pricing_shift` on
2026-01-01. Production customers' true outcomes are stored in a separate `simulation` schema
and revealed only by the outcome-resolution command. Monitoring and retraining never read the
timeline.

**Alternatives considered.**

| Alternative | Why not |
|---|---|
| Static CSV snapshots | no controllable change over time |
| Training new models only from logged predictions | small data, and with a time-aware split every new row would fall into the test period |

**Benefits.** Deterministic scenario; training and production share one code path; the
platform must *discover* change from data, as in reality.

**Trade-offs and limits.** Retraining data comes from the simulated warehouse, not from the
logged predictions.

---

## 8. Monitor each version against its own reference

**Context.** Prediction drift only makes sense when production predictions are compared with
predictions of the same model on known data.

**Decision.** The `evaluate` stage saves the test-period rows together with the model's own
predictions as `reference/reference.parquet`. Monitoring compares the newest observations of
the version that produced them with that version's reference.

**Benefits.** Prediction drift is meaningful; after a promotion, the new champion starts
with a clean history instead of inheriting its predecessor's drift.

**Trade-offs and limits.** A version without a reference file (not produced by this
pipeline) cannot be monitored.

---

## 9. Fixed drift methods

**Context.** Evidently chooses statistical tests automatically based on the number of rows
(p-value tests for small samples, distances for large ones). The meaning of "drifted" would
then change between runs.

**Decision.** Always use the normalised **Wasserstein distance** for numeric columns and the
**Jensen-Shannon distance** for categorical and boolean columns, with a threshold of 0.1.

**Benefits.** Comparable results for any window size; distances do not become
hypersensitive with large samples the way p-values do. Two samples of unchanged data give
distances of 0.00-0.07, comfortably below 0.1.

**Trade-offs and limits.** One global threshold for all features; no per-feature tuning.

---

## 10. A retraining policy with severity, cooldown and one open request

**Context.** Retraining on every drift signal wastes compute, can chase noise, and can
create endless loops while the same drift keeps being reported.

**Decision.** Retraining is requested only for strong evidence - at least 25% of the features
drifted **and** the prediction distribution moved, or measured accuracy dropped - and never
on data flagged for quality problems. A database index allows only one open request per
model, and a 12-hour cooldown spaces requests.

**Benefits.** Retraining happens when it is justified and exactly once per episode.

**Trade-offs and limits.** A genuine but mild change is only "watched" until it becomes
severe or outcomes show an accuracy drop. Operators can always request retraining manually.

---

## 11. Load once, retry only until loaded, reload explicitly

**Context.** A prediction service must never serve from a missing or unverified model, and
swapping models inside a running process is error-prone.

**Decision.** At startup the API resolves the `champion` alias to a concrete version,
verifies and loads it, and never replaces it while running. If loading fails, the process
stays alive, reports "not ready" and retries every 15 seconds **until the first success**.
A new champion is served after `churnctl serving reload`.

**Alternatives considered.** Hot reload on alias change (risk of half-swapped state, harder
to reason about during incidents); crash on load failure (container restart loops, no
readable reason).

**Benefits.** Simple, observable behaviour; recovers by itself when the registry comes up
late.

**Trade-offs and limits.** Promotion and rollback need one extra command.

---

## 12. Every served prediction is recorded

**Context.** Monitoring and accuracy measurement depend on complete prediction logs;
silently losing some would bias them.

**Decision.** `observations.on_persistence_failure: reject` - if a prediction cannot be
stored, the API answers 503 and readiness fails until the database is back.

**Alternatives considered.** `serve` mode (answer anyway and count the loss) - available as a
one-line configuration change for cases where availability matters more.

**Trade-offs and limits.** A database outage makes predictions unavailable in the default
mode. The connection pool validates connections before use, so a database *restart* does
not fail the next request.

---

## 13. Lifecycle operations are local commands

**Decision.** Registration, gating, promotion, rollback and retraining exist only as
`churnctl` commands. The public API exposes prediction and status routes only.

**Benefits.** No network caller can change which model serves customers; the operator's
machine is the trust boundary.

**Trade-offs and limits.** Remote administration would need a separate, authenticated
administration service.

---

## 14. One PostgreSQL server, separate databases and roles

**Decision.** One server with databases `mlflow` and `churn_ops`, and roles `mlflow`,
`churn_ops` (owner), `churn_inference` (insert predictions only) and `grafana_reader`
(read-only).

**Alternatives considered.** Separate database servers per component (more memory and
operations for no benefit locally).

**Benefits.** Logical separation and least privilege at the cost of one container.

**Trade-offs and limits.** One server is a shared failure point for tracking and operations.

---

## 15. Segments are compared on the primary metric

**Context.** An overall improvement can hide a loss for one group of customers, so the gate
compares every important segment with the champion. The question is *which* metric to
compare.

**Decision.** Per-segment comparison uses **F1** only; recall is protected by an absolute
per-segment floor (0.25).

**Alternatives considered.** Comparing recall per segment as well. With the demo data, such
a rule rejects the retrained model although it has higher F1 in every segment and ROC-AUC
+0.09: the old model over-predicts churn among basic-plan customers after the pricing
change - many alarms, therefore high recall but poor precision. A candidate that trades a
little recall for much better precision can be the better model.

**Trade-offs and limits.** A drop in recall that keeps F1 stable and stays above the floor is
accepted.

---

## 16. Production accuracy uses the training metric code

**Decision.** Delayed accuracy is computed with `churn_platform.training.metrics` from the
predictions that were actually served; Evidently is used for distribution analysis
(drift, data quality) and reports.

**Benefits.** Production and test-period metrics come from the same implementation, so
comparing them is meaningful.

---

## 17. The monitoring worker skips unchanged data

**Context.** Each Evidently HTML report is about 4.5 MB. A worker running every five minutes
would store more than 1 GB of identical reports per day.

**Decision.** Each monitoring run stores an *input fingerprint* (number and newest time of
predictions and of resolved outcomes). The worker skips a cycle when the fingerprint has not
changed; a manual `churnctl monitor run` always analyses.

**Trade-offs and limits.** Reports are still kept for every analysed cycle; there is no
retention policy.

---

## 18. Deterministic randomness everywhere

**Decision.** Every random process has a fixed seed: the data generator (`generation.seed`),
the traffic simulator (derived from its dates and profile unless `--seed` is given) and all
estimators (`random_state: 42`).

**Benefits.** Re-running the pipeline produces byte-identical dataset files (DVC sees no
change) and the same models; the documentation's numbers are reproducible.

**Trade-offs and limits.** Re-sending traffic with the same dates replays the same customers;
pass `--seed` for new ones.
