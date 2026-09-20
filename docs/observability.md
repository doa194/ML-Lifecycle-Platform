# Observability: metrics, alerts and dashboards

> **In short:** Prometheus scrapes the inference service every 5 seconds and evaluates five
> alert rules; Grafana shows two provisioned dashboards - one for **operations** (is the
> service up, fast, error-free, serving the right model?) and one for **ML monitoring**
> (drift, delayed accuracy, retraining and the lifecycle audit trail, read from PostgreSQL).
> Operational and statistical signals are deliberately kept apart.

## Contents

- [Two kinds of signals](#two-kinds-of-signals)
- [Prometheus metrics](#prometheus-metrics)
- [Scraping and retention](#scraping-and-retention)
- [Alert rules](#alert-rules)
- [Grafana dashboards](#grafana-dashboards)
- [Useful queries](#useful-queries)
- [Logs](#logs)
- [Verified behaviour](#verified-behaviour)

---

## Two kinds of signals

| | Operational signals | Statistical (ML) signals |
|---|---|---|
| Questions | Is the service up? How fast? How many errors? Which model is loaded? | Has the data changed? Have the predictions changed? Is the model still accurate? |
| Collected by | Prometheus, from the API's `/metrics` endpoint | the monitoring worker, with Evidently |
| Stored in | Prometheus time-series storage | PostgreSQL `ops.monitoring_runs` (+ reports in MLflow) |
| Shown in | Grafana *Churn Inference Operations* | Grafana *Churn Model Monitoring* |
| Details | this document | [ml-monitoring.md](ml-monitoring.md) |

Prometheus never computes drift, and the monitoring worker never measures latency. Each tool
does what it is good at.

---

## Prometheus metrics

All metrics are defined in `churn_platform/serving/metrics.py`.

### Traffic and latency

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `churn_http_requests_total` | counter | `method`, `route`, `status` | every HTTP request; `status` 200 = served, 422 = invalid input, 503 = not ready or not recorded |
| `churn_http_request_duration_seconds` | histogram | `method`, `route` | end-to-end request time |
| `churn_model_inference_seconds` | histogram | - | time spent inside the model pipeline only |

### What the model predicts

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `churn_predictions_total` | counter | `prediction` (`churn` / `no_churn`), `risk_level`, `model_version` | served predictions |
| `churn_prediction_probability` | histogram (buckets 0.1 ... 1.0) | - | distribution of predicted probabilities |

### Model state

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `churn_model_loaded` | gauge | - | 1 when a verified champion is loaded, else 0 |
| `churn_model_info` | gauge (always 1) | `model_name`, `model_version`, `run_id`, `algorithm` | identity of the loaded model |
| `churn_model_loaded_timestamp_seconds` | gauge | - | when it was loaded |
| `churn_model_load_failures_total` | counter | `reason` (`integrity`, `no_champion`, `registry_unavailable`, `error`) | failed load attempts |
| `churn_observation_write_failures_total` | counter | - | predictions that could not be stored (gaps for monitoring) |

### Label rules

- **Routes are templates** (`/predict`), never raw URLs; unknown paths are labelled
  `unmatched`.
- **No customer or prediction IDs as labels.** Every distinct label value creates a separate
  time series; per-customer labels would overwhelm Prometheus (a component test checks this).
- `model_version` changes only when a new model is deployed, so it adds very few series.

---

## Scraping and retention

`deploy/prometheus/prometheus.yml`:

| Job | Target | Interval |
|---|---|---|
| `inference` | `inference:8000/metrics` | 5 seconds |
| `prometheus` | Prometheus itself | 10 seconds |

Metrics are kept for **7 days** in the `prometheus-data` volume. Prometheus UI:
http://127.0.0.1:9090 (*Status -> Targets* should show the `inference` target as `UP`).

---

## Alert rules

`deploy/prometheus/rules/inference.yml`. They are evaluated every 15 seconds and shown on
Prometheus' *Alerts* page. No Alertmanager is deployed, so alerts are visible but not sent
anywhere.

| Alert | Fires when | Severity | What to do |
|---|---|---|---|
| `InferenceDown` | Prometheus cannot scrape the service for 1 minute | critical | `uv run churnctl stack status`; see [failure-recovery.md](failure-recovery.md) |
| `ChampionNotLoaded` | `churn_model_loaded == 0` for 2 minutes | critical | `uv run churnctl serving status` shows the reason |
| `HighPredictionErrorRate` | more than 5% of `/predict` requests answered 5xx over 5 minutes | warning | check readiness and the database |
| `HighPredictionLatency` | p95 `/predict` latency above 250 ms for 5 minutes | warning | check host load and container memory |
| `ObservationsNotRecorded` | any failed prediction write in 5 minutes | warning | check PostgreSQL; monitoring data has gaps |

---

## Grafana dashboards

Grafana runs at http://127.0.0.1:3000. Both dashboards live in the *Churn Platform* folder
and are provisioned from `deploy/grafana/` (read-only in the UI; edit the JSON files to
change them). Viewing needs no login; editing requires the admin user (`admin`, password
`GRAFANA_ADMIN_PASSWORD` in `.env`).

| Data source | Type | Connects as |
|---|---|---|
| `Prometheus` | Prometheus | - |
| `Churn Ops` | PostgreSQL | role `grafana_reader` (read-only on the `ops` tables) |

### Churn Inference Operations (Prometheus, refreshes every 10 s)

Organised by the questions an on-call engineer asks:

| Row | Panels | Tells you |
|---|---|---|
| **Is the service serving the right model?** | Champion loaded - Serving model version - Loaded since - Scrape target - Model load failures (1 h) - Unrecorded predictions (1 h) | whether a verified model is loaded, which one, since when, and whether loading or recording is failing |
| **Is it healthy?** | Prediction requests/s - Server error rate - Rejected invalid input - p95 latency - Requests by status - Latency percentiles (request vs. model only) | traffic, error ratios and where time is spent |
| **What is it predicting?** | Predictions by risk level - Predicted churn share (15 min) - Churn probability distribution | the mix of answers; a sudden change here is often the first visible sign of drift |

### Churn Model Monitoring (PostgreSQL, refreshes every 30 s)

| Row | Panels | Tells you |
|---|---|---|
| **Latest monitoring result** | Policy decision - Served version monitored - Observations in window - Features drifted - Prediction drift score - Data quality - Drift over time - Feature drift scores (per feature) | what monitoring concluded and why |
| **Delayed performance** | Resolved outcomes - Production F1 - Test F1 at training time - Production ROC-AUC - Performance over time | the model's real accuracy once outcomes are known |
| **Continuous training and lifecycle** | Retraining requests - Lifecycle audit trail | what retraining did and every registry decision |

A **Registered model** selector at the top switches between registered model names.

---

## Useful queries

Paste into Prometheus (*Graph*) or Grafana (*Explore*):

| Question | PromQL |
|---|---|
| prediction requests per second | `sum(rate(churn_http_requests_total{route="/predict"}[1m]))` |
| share of rejected input (5 min) | `sum(rate(churn_http_requests_total{route="/predict",status="422"}[5m])) / sum(rate(churn_http_requests_total{route="/predict"}[5m]))` |
| p95 request latency | `histogram_quantile(0.95, sum by (le) (rate(churn_http_request_duration_seconds_bucket{route="/predict"}[5m])))` |
| predicted churn share (15 min) | `sum(increase(churn_predictions_total{prediction="churn"}[15m])) / sum(increase(churn_predictions_total[15m]))` |
| which model is served | `churn_model_info` |

---

## Logs

Every service logs to standard output:

```bash
docker compose logs --tail 100 inference
```

| Service | Useful lines |
|---|---|
| `inference` | `serving customer-churn-classifier version N` after a successful load; `champion model could not be loaded: <reason>` on failure; `prediction <id> not recorded` on database problems |
| `monitoring-worker` | one JSON summary per analysed cycle; `cycle skipped` when nothing changed; `monitoring cycle failed; retrying` on errors |
| `mlflow` | HTTP access log of the tracking server |

---

## Verified behaviour

Operational tests (`tests/operational/test_observability.py`) check against the running
stack that:

- Prometheus scrapes the service and reports `churn_model_loaded = 1`;
- the alert rules are loaded;
- sending traffic moves the served (200) and rejected (422) counters by **exactly** the number
  of requests sent;
- both Grafana data sources are healthy and both dashboards exist.

A component test checks that the metrics endpoint exposes the expected series and no
customer identifiers.
