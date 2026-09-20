"""FastAPI inference service: online churn predictions from the registry champion.

Responsibilities are deliberately narrow. This process only:
  * loads the champion once at startup (see `model_state`)
  * validates requests, predicts, records observations and exposes metrics
It never trains, evaluates drift, or changes registry aliases; those are separate local
workflows, so a problem there cannot affect serving and vice versa.

Endpoints: POST /predict, GET /health (liveness), GET /ready (readiness), GET /model
(lineage of the served model), GET /metrics (Prometheus).
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from churn_platform.config import ServingConfig, load_serving_config
from churn_platform.serving.metrics import ServingMetrics
from churn_platform.serving.model_state import (
    ChampionLoader,
    ModelHolder,
    ServingModel,
    mlflow_champion_loader,
)
from churn_platform.serving.observations import (
    ObservationStore,
    PostgresObservationStore,
    PredictionObservation,
)
from churn_platform.serving.risk import classify_risk
from churn_platform.serving.schemas import (
    PredictionRequest,
    PredictionResponse,
    to_frame,
)
from churn_platform.settings import Settings, load_settings

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    serving_config: ServingConfig | None = None,
    loader: ChampionLoader | None = None,
    store: ObservationStore | None = None,
) -> FastAPI:
    """Build the app. Tests inject a fake loader/store; production uses MLflow + PostgreSQL."""
    settings = settings or load_settings()
    config = serving_config or load_serving_config(settings.config_dir)
    metrics = ServingMetrics()
    if loader is None:
        from churn_platform.tracking.mlflow_setup import configure_mlflow

        configure_mlflow(settings)
        loader = mlflow_champion_loader(settings.mlflow_tracking_uri, settings.model_name)
    store = store or PostgresObservationStore(settings.ops_database_url)

    def on_loaded(model: ServingModel) -> None:
        metrics.model_loaded.set(1)
        metrics.model_loaded_at.set(model.loaded_at.timestamp())
        metrics.model_info.labels(model.name, model.version, model.run_id, model.tags.get("training.algorithm", "unknown")).set(1)

    holder = ModelHolder(loader, on_failure=lambda reason: metrics.model_load_failures.labels(reason).inc(), on_success=on_loaded)
    metrics.model_loaded.set(0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.open()
        if not holder.try_load():
            holder.retry_in_background(config.model_loading.retry_interval_seconds)
        yield
        holder.stop()
        store.close()

    app = FastAPI(
        title="Churn inference API",
        version="1.0.0",
        description="30-day voluntary churn risk for active subscription customers, served from the MLflow champion.",
        lifespan=lifespan,
    )
    app.state.holder = holder
    app.state.metrics = metrics

    @app.middleware("http")
    async def observe(request: Request, call_next):
        # Reject oversized bodies before they are read or parsed.
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > config.max_request_bytes:
            metrics.requests.labels(request.method, "oversized", "413").inc()
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - never leak internals to clients; count it as a 500
            log.exception("unhandled error on %s", request.url.path)
            response = JSONResponse({"detail": "internal error"}, status_code=500)
        # Route templates (e.g. "/predict") keep metric labels bounded.
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.request_seconds.labels(request.method, route).observe(time.perf_counter() - start)
        metrics.requests.labels(request.method, route, str(response.status_code)).inc()
        return response

    def not_ready(reason: str) -> JSONResponse:
        return JSONResponse({"status": "not_ready", "reason": reason}, status_code=503)

    @app.post("/predict", response_model=PredictionResponse, responses={503: {"description": "no verified model loaded or prediction not recorded"}})
    def predict(payload: PredictionRequest):
        served = holder.current
        if served is None:
            return not_ready("no verified champion model is loaded")
        start = time.perf_counter()
        probability = float(served.model.predict_proba(to_frame(payload.features))[0])
        metrics.inference_seconds.observe(time.perf_counter() - start)
        threshold = served.model.decision_threshold
        prediction = probability >= threshold
        risk = classify_risk(probability, threshold, config.risk_levels.medium_ratio)
        snapshot_date = payload.snapshot_date or datetime.now(UTC).date()
        observation = PredictionObservation(
            prediction_id=uuid.uuid4(),
            customer_id=payload.customer_id,
            snapshot_date=snapshot_date,
            features=payload.features.model_dump(mode="json"),
            churn_probability=probability,
            churn_prediction=int(prediction),
            risk_level=risk,
            model_name=served.name,
            model_version=served.version,
            model_run_id=served.run_id,
        )
        try:
            store.record(observation)
        except Exception as error:  # noqa: BLE001 - behaviour is decided by explicit policy
            metrics.observation_failures.inc()
            log.error("prediction %s not recorded: %s", observation.prediction_id, error)
            if config.observations.on_persistence_failure == "reject":
                return JSONResponse({"detail": "prediction could not be recorded; retry later"}, status_code=503)
        metrics.predictions.labels("churn" if prediction else "no_churn", risk, served.version).inc()
        metrics.probability.observe(probability)
        return PredictionResponse(
            prediction_id=observation.prediction_id,
            customer_id=payload.customer_id,
            snapshot_date=snapshot_date,
            churn_prediction=prediction,
            churn_probability=round(probability, 6),
            risk_level=risk,
            decision_threshold=threshold,
            model_name=served.name,
            model_version=served.version,
        )

    @app.get("/health")
    def health():
        """Liveness: the process is up. Says nothing about whether it can predict."""
        return {"status": "alive", "model_loaded": holder.ready}

    @app.get("/ready")
    def ready():
        """Readiness: a verified model is loaded (and, in strict mode, observations can be stored)."""
        if not holder.ready:
            return not_ready(holder.last_error or "champion model is still loading")
        if config.observations.on_persistence_failure == "reject" and not store.healthy():
            return not_ready("observation store unavailable")
        return {"status": "ready", "model_name": holder.current.name, "model_version": holder.current.version}

    @app.get("/model")
    def model_info():
        """Lineage of the served model: registry version -> run -> data -> code."""
        served = holder.current
        if served is None:
            return not_ready(holder.last_error or "champion model is still loading")
        tags = served.tags
        return {
            "model_name": served.name,
            "model_version": served.version,
            "alias": "champion",
            "loaded_at": served.loaded_at.isoformat(),
            "run_id": served.run_id,
            "algorithm": tags.get("training.algorithm"),
            "decision_threshold": served.model.decision_threshold,
            "fingerprint_sha256": served.model.fingerprint,
            "feature_schema_version": tags.get("feature_schema_version"),
            "training_cycle_id": tags.get("training.cycle_id"),
            "dataset": {
                "id": tags.get("data.id"),
                "as_of": tags.get("data.as_of"),
                "train_md5": tags.get("data.train_md5"),
                "test_md5": tags.get("data.test_md5"),
            },
            "code": {"git_commit": tags.get("code.git_commit"), "git_dirty": tags.get("code.git_dirty")},
            "quality_gate": {
                "status": tags.get("gate.status"),
                "evaluated_at": tags.get("gate.evaluated_at"),
                "compared_with_champion": tags.get("gate.champion_version"),
            },
            "promoted_at": tags.get("lifecycle.promoted_at"),
            "validation_metrics": {k: served.run_metrics.get(f"val_{k}") for k in ("f1", "recall", "roc_auc")},
            "test_metrics": {k: served.run_metrics.get(f"test_{k}") for k in ("f1", "recall", "precision", "roc_auc")},
        }

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics():
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app
