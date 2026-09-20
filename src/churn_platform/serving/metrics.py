"""Prometheus metrics of the inference service (operational behaviour, not statistical drift).

Labels are kept low-cardinality on purpose: route templates instead of raw paths, and never
customer or prediction IDs, which would create one time series per request.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


class ServingMetrics:
    """All metrics live in one registry per app instance, which keeps tests independent."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.requests = Counter(
            "churn_http_requests_total", "HTTP requests by route and status code", ["method", "route", "status"], registry=r
        )
        self.request_seconds = Histogram(
            "churn_http_request_duration_seconds", "End-to-end HTTP request latency", ["method", "route"],
            buckets=LATENCY_BUCKETS, registry=r,
        )
        self.predictions = Counter(
            "churn_predictions_total", "Served predictions by outcome", ["prediction", "risk_level", "model_version"], registry=r
        )
        self.probability = Histogram(
            "churn_prediction_probability", "Distribution of predicted churn probabilities",
            buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0), registry=r,
        )
        self.inference_seconds = Histogram(
            "churn_model_inference_seconds", "Time spent inside the model pipeline", buckets=LATENCY_BUCKETS, registry=r
        )
        self.model_load_failures = Counter(
            "churn_model_load_failures_total", "Failed attempts to load the champion model", ["reason"], registry=r
        )
        self.model_loaded = Gauge("churn_model_loaded", "1 when a verified champion model is loaded", registry=r)
        self.model_info = Gauge(
            "churn_model_info", "Identity of the loaded model (value is always 1)",
            ["model_name", "model_version", "run_id", "algorithm"], registry=r,
        )
        self.model_loaded_at = Gauge(
            "churn_model_loaded_timestamp_seconds", "Unix time when the current model was loaded", registry=r
        )
        self.observation_failures = Counter(
            "churn_observation_write_failures_total", "Predictions that could not be recorded in PostgreSQL", registry=r
        )
