# Production considerations

> **In short:** This platform is *production-style*, not a production deployment. The
> lifecycle rules, safety checks, permissions and failure behaviour are the ones a real system
> needs, and they are implemented and tested. The infrastructure, however, is deliberately
> one machine: single instances, no logins on internal tools, secrets in a local file,
> simulated data. This document separates three things clearly: **what is implemented now**,
> **what is simplified for local use and why**, and **what would have to change** before real
> customers depended on it. Nothing in the last category exists in this repository.

## Contents

- [Implemented now](#implemented-now)
- [Simplified for local use](#simplified-for-local-use)
- [What would change for production](#what-would-change-for-production)
  - [Infrastructure](#infrastructure)
  - [Serving and rollout](#serving-and-rollout)
  - [Security](#security)
  - [Pipelines and orchestration](#pipelines-and-orchestration)
  - [Data](#data)
  - [Monitoring and alerting](#monitoring-and-alerting)
  - [Governance and delivery](#governance-and-delivery)
- [Scaling limits of the current design](#scaling-limits-of-the-current-design)
- [What carries over unchanged](#what-carries-over-unchanged)

---

## Implemented now

Everything in this table exists in the repository and is covered by tests or operational
checks ([testing-strategy.md](testing-strategy.md)).

| Area | Implemented behaviour | Details |
|---|---|---|
| data | deterministic dataset generation, 13 validation checks, leakage-safe time split with a purge gap | [data-pipeline.md](data-pipeline.md), [leakage-controls.md](leakage-controls.md) |
| versioning | every dataset version stored in DVC with a MinIO remote and restorable from Git | [dataset-versioning.md](dataset-versioning.md) |
| training | three algorithms trained reproducibly; threshold tuned on validation data; one MLflow run per algorithm with full lineage | [training-and-evaluation.md](training-and-evaluation.md), [experiment-tracking.md](experiment-tracking.md) |
| lifecycle | registry aliases `candidate` / `challenger` / `champion` with an explicit state machine; independent quality gate with 9 checks; promotion, rollback and an audit trail | [model-lifecycle.md](model-lifecycle.md), [quality-gate.md](quality-gate.md) |
| model safety | safe model format (skops) and a fingerprint check on every load | [security.md](security.md#model-artifact-integrity) |
| serving | strict request validation, honest readiness, every prediction recorded with its model version, Prometheus metrics | [serving.md](serving.md), [api-reference.md](api-reference.md) |
| monitoring | drift, data-quality and prediction-drift analysis with Evidently; delayed performance once outcomes arrive; Grafana dashboards; five Prometheus alert rules | [ml-monitoring.md](ml-monitoring.md), [observability.md](observability.md) |
| continuous training | a retraining policy with severity, cooldown and de-duplication; a controller that cannot bypass the gate and never disturbs serving | [continuous-training.md](continuous-training.md) |
| security | loopback-only ports, per-component database roles and storage keys, hardened containers, no lifecycle actions on the API | [security.md](security.md) |
| recovery | automatic reconnection and model-load retry; failures isolated from the champion | [failure-recovery.md](failure-recovery.md) |

---

## Simplified for local use

These are deliberate choices that keep the platform runnable on a laptop with one command.
Each is acceptable *here* for the stated reason, and each is listed again below with its
production replacement.

| Area | Local choice | Why it is acceptable locally |
|---|---|---|
| runtime | one Docker Compose stack; one instance of every service | fits in about 5 GB of memory on one machine |
| serving capacity | one uvicorn process; a restart loads a new champion (a few seconds of unavailability) | predictable and easy to observe |
| authentication | none on MLflow, Prometheus and the API; Grafana dashboards viewable anonymously | only this machine can reach the ports |
| secrets | random values in `.env`, passed to containers as environment variables | nothing leaves the machine |
| transport | plain HTTP between containers and to the browser | no untrusted network in between |
| orchestration | the DVC pipeline and the retraining controller run as local commands; the monitoring worker is a simple loop | no scheduler to install or operate |
| workspace changes | retraining writes the new `params.yaml` / `dvc.lock` into the working tree for the operator to commit | the operator sees and approves every change |
| data | synthetic "warehouse" extracts, simulated traffic and simulated outcomes | drift is controllable and every run is reproducible ([synthetic-world.md](synthetic-world.md)) |
| monitoring windows | the newest 1,000 predictions of the served version | works for any traffic volume in demos |
| alerting | Prometheus alert rules without Alertmanager | alerts are visible in Prometheus but not sent anywhere |
| retention | runs, reports and dataset versions are never deleted automatically; metrics are kept 7 days | disk use stays small in demos |
| backups | manual copies of Docker volumes ([operations.md](operations.md#back-up-and-restore)) | the data can be regenerated |

---

## What would change for production

Everything in this section is a **recommendation**, not implemented functionality.

### Infrastructure

| Now | In production |
|---|---|
| Docker Compose on one machine | Kubernetes or a managed container platform, with several nodes |
| PostgreSQL container with a local volume | managed PostgreSQL with automated backups, point-in-time recovery and a standby replica |
| MinIO container | managed object storage (or a replicated MinIO cluster) with versioning and lifecycle rules |
| fixed memory limits | resource requests and limits per service, with autoscaling for the API |

### Serving and rollout

| Now | In production |
|---|---|
| one API process | several replicas behind a load balancer that routes on `/ready` |
| restart to load a new champion | rolling or blue/green rollout: new replicas load the new champion and receive traffic only once ready, so there is no gap |
| full switch to the new champion | *canary* release (a small share of traffic first) or *shadow* traffic (the new model scores silently beside the old one), with automatic rollback on error-rate or latency alerts |
| synchronous single predictions | the same, plus a batch scoring job for weekly retention campaigns if needed |

The API itself would not need to change: it is already stateless per request, loads one
verified model, and reports honest readiness - the properties rolling deployments require.

### Security

| Now | In production |
|---|---|
| no login on MLflow | authentication and authorisation for MLflow (or an authenticating proxy), with a separate identity per component, so serving and monitoring are *technically* unable to move aliases |
| no login on the API | authentication for API clients (for example service tokens or mutual TLS) and rate limiting |
| plain HTTP | TLS for every connection |
| `.env` file and environment variables | a secret manager (for example Vault or a cloud secret store) with rotation |
| loopback-only ports | network policies that allow only the connections the architecture needs |
| fingerprint check | fingerprint check **plus** signed model artifacts, which also prove who produced a model |
| manual `pip-audit` | dependency and container image scanning in CI, blocking releases with fixable critical findings |
| MinIO running as root | a non-root object storage deployment |

### Pipelines and orchestration

| Now | In production |
|---|---|
| DVC stages run by `churnctl pipeline run` | the same stage functions run as steps of a workflow engine such as Kubeflow Pipelines or Airflow (the stages are plain Python functions with file inputs and outputs, which makes this move straightforward) |
| retraining controller run by hand | a scheduled or event-driven job, triggered when a retraining request appears |
| controller changes the working tree | the controller works in its own checkout and opens a pull request with the new `params.yaml` / `dvc.lock`, so dataset changes are reviewed like code |
| monitoring worker loop | a scheduled job per model, with one run per calendar window |

### Data

| Now | In production |
|---|---|
| synthetic extracts | extracts from the real data warehouse, with the same validation checks and leakage rules |
| simulated outcomes | cancellations joined from billing events, with the same "outcome known only after 30 days" rule |
| features computed in the model pipeline | unchanged, unless several models share features or need point-in-time lookups at scale - only then would a feature store (such as Feast) be worth its operational cost |
| no personal data | customer data would require access controls, retention limits and data-protection review; predictions store features, so they would be treated as personal data |

### Monitoring and alerting

| Now | In production |
|---|---|
| alerts visible in Prometheus | Alertmanager routing alerts to an on-call rotation, with runbooks linked from each alert |
| "newest 1,000 predictions" window | calendar-based windows (for example weekly), which make trends comparable over time |
| one drift threshold for all features | per-feature thresholds tuned on historical variation |
| reports kept forever | retention policy for monitoring runs and reports |
| model metrics only | business metrics next to model metrics - for example how many flagged customers were contacted and how many stayed |

### Governance and delivery

| Now | In production |
|---|---|
| automatic promotion allowed | `approval.manual_approval_required: true`, so a named person approves every new champion |
| tests run by hand | CI running unit and component tests on every change and the integration and end-to-end suites before each release |
| model version metadata in MLflow | a *model card* per version: intended use, training data, known weaknesses, fairness review of segment results |
| audit trail in PostgreSQL | the same trail, exported to long-term, tamper-evident storage |

---

## Scaling limits of the current design

These limits come from the design itself, not only from running on one machine:

| Limit | Effect | What would change it |
|---|---|---|
| one registry alias per role, one open retraining request per model | fine for a handful of models; not a multi-team model platform | per-team registries or namespaces |
| monitoring analyses 1,000 predictions per cycle in memory | fast and simple; cannot analyse millions of rows at once | sampling, or a data-processing engine for monitoring |
| retraining runs the full pipeline from raw data | simple and reproducible; slow for very large datasets | incremental extracts and cached intermediate stages |
| every prediction is written to PostgreSQL synchronously | guarantees a record per prediction; limits throughput to what one database accepts | an append-only event stream (for example Kafka) feeding the monitoring store |
| models are loaded into each API process | fine for small scikit-learn models; large models would need dedicated model servers | a model server such as KServe or Triton |

---

## What carries over unchanged

Most of the platform's value is in rules that do not depend on the infrastructure:

- the quality gate, its policy file and the rule that only the gate can create a challenger;
- the alias state machine, promotion blockers and rollback rules;
- the fingerprint check on every model load;
- the leakage-safe time split and the "outcome known only after 30 days" rule;
- the retraining policy (minimum evidence, severity, cooldown, one open request);
- the lineage chain from prediction -> model version -> MLflow run -> dataset version.

Moving to production means replacing *where* these rules run, not *what* they decide.

## Related documents

- [architecture.md](architecture.md) - the current architecture
- [design-decisions.md](design-decisions.md) - why the current choices were made
- [security.md](security.md#known-limits) - current security limits
