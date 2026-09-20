# Failure modes and recovery

> **In short:** This document lists what happens when each part of the platform fails,
> whether the platform recovers by itself, and what to do when it does not. The central
> guarantees: a loaded champion keeps serving through registry outages, a service without a
> verified model says "not ready" instead of guessing, a failed candidate or retraining never
> removes the champion, and data survives container restarts. Most rows are verified by
> automated tests that break real components.

## Contents

- [Design principles for failure](#design-principles-for-failure)
- [Infrastructure failures](#infrastructure-failures)
- [Model and lifecycle failures](#model-and-lifecycle-failures)
- [Monitoring failures](#monitoring-failures)
- [Standard recovery procedures](#standard-recovery-procedures)

---

## Design principles for failure

| Principle | How it shows up |
|---|---|
| **Fail closed on models** | no verified model -> not ready; damaged model -> refused everywhere |
| **Keep serving what works** | once loaded, the champion does not depend on MLflow or MinIO |
| **Isolate experiments from production** | training, gate and retraining failures cannot move the `champion` alias |
| **Recover automatically where it is safe** | the API retries its first model load; database connections are re-validated; containers restart with Docker |
| **Make failures visible** | readiness reasons, Prometheus counters and alerts, failed requests with error text |
| **Retry safely** | every lifecycle step is idempotent |

---

## Infrastructure failures

| Failure | Effect | Recovery | Verified |
|---|---|---|---|
| **PostgreSQL restarts** | during the restart `/predict` answers 503 (default `reject` mode) and `/ready` fails; afterwards the connection pool replaces broken connections before use, so the next prediction succeeds | automatic | operational test |
| **PostgreSQL down for a longer time** | predictions 503; MLflow unhealthy; monitoring cycles fail and are retried every interval; lifecycle commands fail with connection errors | `uv run churnctl stack up`; everything resumes | - |
| **MLflow down while a champion is loaded** | **no effect on serving** - the API never calls MLflow after startup; training, lifecycle commands and monitoring fail with connection errors | `uv run churnctl stack up`, then retry the commands | operational test |
| **MLflow down while the API starts** | the API is alive but not ready (reason `registry_unavailable`); `/predict` answers 503; the load-failure counter increases | automatic: the API retries every 15 s and becomes ready once MLflow is healthy | operational test |
| **MinIO down** | the loaded champion keeps serving; new model loads fail (not ready); `dvc push`/`pull` fail | `uv run churnctl stack up` | - |
| **Any container restarted** | metadata, artifacts and predictions persist in volumes | automatic | operational test (PostgreSQL, MinIO and MLflow restarted; runs and artifacts still readable) |
| **Docker or the machine restarted** | services with `restart: unless-stopped` start again with Docker; the API loads the champion at startup | automatic | observed manually |

---

## Model and lifecycle failures

| Failure | Effect | Recovery | Verified |
|---|---|---|---|
| **Champion model file modified or corrupted** | load refused with `ModelIntegrityError`: the API is not ready, the gate's `loadable_and_intact` check fails, promotion and rollback to that version are refused | roll back to a healthy version and reload serving | operational test (corrupts a real file in MinIO) |
| **Champion model file deleted** | load fails; the API is not ready | same | operational test |
| **No champion yet** | the API is alive, not ready (`no_champion`) | register, gate and promote a model | component test |
| **Candidate fails the gate** | the version becomes `rejected`; the champion keeps serving | train a better model, or change the policy and re-run the gate | integration test |
| **Challenger became stale** (the champion changed after its gate run) | promotion refused: "re-run the gate" | `uv run churnctl model gate --version N` | unit test |
| **A pipeline stage fails during retraining** | the request becomes `failed` with the error; no version is registered; champion and serving unchanged | fix the cause, then `uv run churnctl retrain run --request-id <id>` | integration test |
| **The controller process dies mid-request** | the request stays `running` | it becomes claimable again after `stale_after_minutes` (60); re-processing is safe because every step is idempotent | partly (integration tests cover single claims and the safe retry of a failed request) |
| **The audit event cannot be written** | the registry change stands; an error is logged | none needed; the history lacks that row | - |
| **A prediction cannot be stored** | `reject` mode: the prediction is answered with 503 and counted; `serve` mode: the prediction is answered and counted | restore PostgreSQL | component test |
| **The gate cannot load the champion** | the gate stops with an error; nothing changes | roll back to a healthy champion first | - |

---

## Monitoring failures

| Failure | Effect | Recovery |
|---|---|---|
| a monitoring cycle raises an error (for example MLflow unreachable) | logged; the worker keeps running and tries again at the next interval | automatic |
| the monitored version has no reference file | that cycle fails | promote a version produced by this pipeline (every such version has a reference) |
| too few predictions | recorded as `insufficient_data`; no decision | wait for more traffic |
| duplicate or replayed predictions | the data-quality check flags them and retraining is blocked | fix the client that sends duplicates |

---

## Standard recovery procedures

### Serving is not ready

```bash
uv run churnctl serving status
```

| Reason contains | Do |
|---|---|
| `alias champion not found` / `not found` | promote a model, then `uv run churnctl serving reload` |
| `Max retries` / connection errors (`registry_unavailable`) | `uv run churnctl stack up`; the API recovers within 15 s |
| `ModelIntegrityError` | `uv run churnctl model rollback --reason "..."`, then `uv run churnctl serving reload` |
| `observation store unavailable` | `uv run churnctl stack up`; check `docker compose logs postgres` |

### Something is stuck after an outage

```bash
uv run churnctl stack up
```

This is idempotent: it starts whatever is missing, waits for all health checks and re-runs the
one-shot setup containers.

### Start from scratch

```bash
uv run churnctl stack reset --yes
```

Then set `dataset.as_of` in `params.yaml` back to `"2026-01-01"` and run
`uv run churnctl bootstrap`. This deletes all models, runs, predictions and dataset versions.

Symptoms and error messages in more detail: [troubleshooting.md](troubleshooting.md).
