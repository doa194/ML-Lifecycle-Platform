# API reference

> **In short:** The inference API has four documented endpoints - `POST /predict`,
> `GET /health`, `GET /ready`, `GET /model` - plus `GET /metrics` for Prometheus. Base URL:
> `http://127.0.0.1:8000`. There is no authentication (the API is reachable only from this
> machine; see [security.md](security.md)). An interactive version of this reference is
> available at http://127.0.0.1:8000/docs.

## Contents

- [POST /predict](#post-predict)
- [GET /health](#get-health)
- [GET /ready](#get-ready)
- [GET /model](#get-model)
- [GET /metrics](#get-metrics)
- [Status codes](#status-codes)
- [Examples in PowerShell and bash](#examples-in-powershell-and-bash)

---

## POST /predict

Predicts the probability that one active customer voluntarily churns within 30 days of the
snapshot date.

### Request body

```json
{
  "customer_id": "C-1001",
  "snapshot_date": "2025-12-15",
  "features": {
    "plan_tier": "basic",
    "contract_type": "monthly",
    "autopay_enabled": false,
    "recent_price_increase": true,
    "tenure_months": 3,
    "monthly_charge": 24.5,
    "payment_failures_90d": 2,
    "late_payments_12m": 3,
    "monthly_usage_hours": 4.0,
    "sessions_30d": 3,
    "days_since_last_login": 21,
    "usage_trend_pct": -0.4,
    "support_tickets_90d": 2,
    "complaints_90d": 1,
    "avg_resolution_hours": 30.0,
    "features_adopted": 1
  }
}
```

### Fields

| Field | Type | Required | Rule |
|---|---|---|---|
| `customer_id` | string | yes | 1-64 characters: letters, digits, `_`, `.`, `-` |
| `snapshot_date` | date `YYYY-MM-DD` | no | the day the features describe; default: today (UTC) |
| `features.plan_tier` | string | yes | `basic`, `standard` or `premium` |
| `features.contract_type` | string | yes | `monthly`, `annual` or `two_year` |
| `features.autopay_enabled` | boolean | yes | JSON `true` / `false` only (not `"yes"`, not `1`) |
| `features.recent_price_increase` | boolean | yes | price increased in the last 60 days |
| `features.tenure_months` | integer | yes | 0-240 |
| `features.monthly_charge` | number | yes | 1-1000 |
| `features.payment_failures_90d` | integer | yes | 0-30 |
| `features.late_payments_12m` | integer | yes | 0-24 |
| `features.monthly_usage_hours` | number | yes | 0-744 |
| `features.sessions_30d` | integer | yes | 0-3000 |
| `features.days_since_last_login` | integer | yes | 0-365 |
| `features.usage_trend_pct` | number | yes | -1 to 5 (-0.4 = usage fell by 40%) |
| `features.support_tickets_90d` | integer | yes | 0-100 |
| `features.complaints_90d` | integer | yes | 0-100 and **not more than** `support_tickets_90d` |
| `features.avg_resolution_hours` | number or `null` | when there were tickets | 0-2000; **must be given when `support_tickets_90d > 0` and must be `null` when it is 0** |
| `features.features_adopted` | integer | yes | 0-10 |

**Type rules.** Integer fields accept only JSON integers (`3`; not `3.5`, not `"3"`). Number
fields accept integers and decimals. **Unknown fields** anywhere in the body are rejected -
this also prevents outcome fields such as `cancellation_status` from being sent.

### Response 200

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

| Field | Meaning |
|---|---|
| `prediction_id` | ID of the stored prediction (used later to attach the true outcome) |
| `customer_id`, `snapshot_date` | echoed from the request (`snapshot_date` filled in if omitted) |
| `churn_probability` | probability of churn within 30 days, rounded to 6 decimals |
| `churn_prediction` | `true` when `churn_probability >= decision_threshold` |
| `risk_level` | `high` (= churn predicted), `medium` (probability at least 60% of the threshold), `low` |
| `decision_threshold` | the threshold stored in the served model |
| `model_name`, `model_version` | exactly which registered model version answered |

### Error responses

**422 - invalid request** (the model was not called). The body lists every problem with its
location:

```json
{
  "detail": [
    {"type": "literal_error", "loc": ["body", "features", "plan_tier"],
     "msg": "Input should be 'basic', 'standard' or 'premium'", "input": "gold"},
    {"type": "missing", "loc": ["body", "features", "contract_type"], "msg": "Field required"}
  ]
}
```

**503 - no verified model loaded:**

```json
{"status": "not_ready", "reason": "no verified champion model is loaded"}
```

**503 - prediction could not be stored** (default `reject` mode):

```json
{"detail": "prediction could not be recorded; retry later"}
```

**413 - body larger than 16 KB:** `{"detail": "request body too large"}`

**500 - unexpected error:** `{"detail": "internal error"}` (no internal details are exposed)

---

## GET /health

**Liveness.** Returns 200 as long as the process runs, whether or not a model is loaded.

```json
{"status": "alive", "model_loaded": true}
```

---

## GET /ready

**Readiness.** 200 when a verified champion is loaded and - in `reject` mode - the prediction
store answers:

```json
{"status": "ready", "model_name": "customer-churn-classifier", "model_version": "2"}
```

503 otherwise, with the reason. Examples:

```json
{"status": "not_ready", "reason": "RestException: INVALID_PARAMETER_VALUE: Registered model alias champion not found."}
```

```json
{"status": "not_ready", "reason": "ModelIntegrityError: model at models:/customer-churn-classifier/2 has fingerprint 73d5b8a7586a..., expected f65a1c8d0714..."}
```

```json
{"status": "not_ready", "reason": "observation store unavailable"}
```

---

## GET /model

**Lineage of the served model.** 503 while no model is loaded. Example (after the retraining
step of the demo; `git_commit` is `unavailable` when the repository has no commits):

```json
{
  "model_name": "customer-churn-classifier",
  "model_version": "2",
  "alias": "champion",
  "loaded_at": "2026-09-19T13:46:20+00:00",
  "run_id": "4966e8a8d8fd4120a5bdcf76fa80a1f1",
  "algorithm": "logistic_regression",
  "decision_threshold": 0.59,
  "fingerprint_sha256": "<64 hexadecimal characters>",
  "feature_schema_version": "1.0",
  "training_cycle_id": "cycle-20260919T134530Z-b4ff46",
  "dataset": {
    "id": "customer-snapshots@2026-12-31#eb504e60c77f281d",
    "as_of": "2026-12-31",
    "train_md5": "eb504e6020e9842fa8253f0e9c4b9ba6",
    "test_md5": "c77f281d87b8c67f8cf9739a1a09af86"
  },
  "code": {"git_commit": "unavailable", "git_dirty": "unknown"},
  "quality_gate": {"status": "passed", "evaluated_at": "2026-09-19T13:46:03+00:00", "compared_with_champion": "1"},
  "promoted_at": "2026-09-19T13:46:04+00:00",
  "validation_metrics": {"f1": 0.75, "recall": 0.786, "roc_auc": 0.8846},
  "test_metrics": {"f1": 0.7002, "recall": 0.6997, "precision": 0.7007, "roc_auc": 0.851}
}
```

| Field | Meaning |
|---|---|
| `loaded_at` | when this process loaded the model |
| `fingerprint_sha256` | fingerprint verified at load time |
| `training_cycle_id` | the MLflow training cycle that produced the model |
| `dataset` | identity, extract date and content hashes of the training and test data |
| `code` | Git commit and whether the working tree had uncommitted changes |
| `quality_gate` | last gate result and the champion version it was compared with (`none` for the first model) |
| `validation_metrics`, `test_metrics` | scores recorded at training time |

---

## GET /metrics

Prometheus text format; not listed in the OpenAPI schema. The metric catalogue is in
[observability.md](observability.md#prometheus-metrics).

---

## Status codes

| Code | Endpoint(s) | Meaning |
|---|---|---|
| 200 | all | success |
| 413 | `POST /predict` | request body too large |
| 422 | `POST /predict` | request failed validation; the model was not called |
| 500 | all | unexpected internal error |
| 503 | `/predict`, `/ready`, `/model` | not ready (no verified model) or, in `reject` mode, prediction not stored |

---

## Examples in PowerShell and bash

**Prediction - PowerShell:**

```powershell
$features = @{ plan_tier = "basic"; contract_type = "monthly"; autopay_enabled = $false; recent_price_increase = $true; tenure_months = 3; monthly_charge = 24.5; payment_failures_90d = 2; late_payments_12m = 3; monthly_usage_hours = 4.0; sessions_30d = 3; days_since_last_login = 21; usage_trend_pct = -0.4; support_tickets_90d = 2; complaints_90d = 1; avg_resolution_hours = 30.0; features_adopted = 1 }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/predict -ContentType "application/json" -Body (@{ customer_id = "C-1001"; snapshot_date = "2025-12-15"; features = $features } | ConvertTo-Json -Depth 3)
```

**Prediction for a loyal customer - bash:**

```bash
curl -s -X POST http://127.0.0.1:8000/predict -H 'Content-Type: application/json' -d '{"customer_id": "C-1002", "snapshot_date": "2025-12-15", "features": {"plan_tier": "premium", "contract_type": "two_year", "autopay_enabled": true, "recent_price_increase": false, "tenure_months": 48, "monthly_charge": 72.0, "payment_failures_90d": 0, "late_payments_12m": 0, "monthly_usage_hours": 35.0, "sessions_30d": 30, "days_since_last_login": 1, "usage_trend_pct": 0.05, "support_tickets_90d": 0, "complaints_90d": 0, "avg_resolution_hours": null, "features_adopted": 7}}'
```

Returns `"churn_probability": 0.009382`, `"risk_level": "low"`.

**Status - PowerShell / bash:**

```powershell
Invoke-RestMethod http://127.0.0.1:8000/ready
```

```bash
curl -s http://127.0.0.1:8000/model
```
