"""Loads the registry champion once and holds it for the lifetime of the process.

Loading rules:
  * the `champion` alias is resolved to a concrete version first, and that exact version is
    downloaded, so an alias change during startup cannot mix two versions
  * the artifact must match the fingerprint recorded at training time; otherwise the model is
    refused - there is no fallback to another or a local model
  * a failed load leaves the service alive but not ready; a background thread retries until
    the first successful load (this covers MLflow being briefly unavailable at startup)
  * once loaded, the model is never swapped; a new champion requires a service restart
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from churn_platform.tracking.model_io import LoadedModel, ModelIntegrityError

log = logging.getLogger(__name__)
CHAMPION_ALIAS = "champion"


@dataclass
class ServingModel:
    model: LoadedModel
    name: str
    version: str
    run_id: str
    tags: dict[str, str] = field(default_factory=dict)
    run_metrics: dict[str, float] = field(default_factory=dict)
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))


ChampionLoader = Callable[[], ServingModel]


def mlflow_champion_loader(tracking_uri: str, model_name: str) -> ChampionLoader:
    """Build the production loader: registry alias -> verified model -> lineage metadata."""

    def load() -> ServingModel:
        from mlflow.tracking import MlflowClient

        from churn_platform.tracking.model_io import load_model

        client = MlflowClient(tracking_uri)
        version = client.get_model_version_by_alias(model_name, CHAMPION_ALIAS)
        fingerprint = (version.tags or {}).get("model.fingerprint")
        if not fingerprint:
            raise ModelIntegrityError(f"champion version {version.version} has no recorded fingerprint")
        model = load_model(f"models:/{model_name}/{version.version}", expected_fingerprint=fingerprint)
        run = client.get_run(version.run_id)
        return ServingModel(
            model=model,
            name=model_name,
            version=str(version.version),
            run_id=version.run_id,
            tags=dict(version.tags or {}),
            run_metrics=dict(run.data.metrics),
        )

    return load


def failure_reason(error: Exception) -> str:
    """Short, bounded label for the load-failure metric."""
    if isinstance(error, ModelIntegrityError):
        return "integrity"
    text = str(error)
    if "RESOURCE_DOES_NOT_EXIST" in text or "not found" in text.lower():
        return "no_champion"
    if "Connection" in type(error).__name__ or "connection" in text.lower() or "Max retries" in text:
        return "registry_unavailable"
    return "error"


class ModelHolder:
    def __init__(self, loader: ChampionLoader, on_failure: Callable[[str], None], on_success: Callable[[ServingModel], None]):
        self._loader = loader
        self._on_failure = on_failure
        self._on_success = on_success
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.current: ServingModel | None = None
        self.last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.current is not None

    def try_load(self) -> bool:
        with self._lock:
            if self.current is not None:
                return True
            try:
                loaded = self._loader()
            except Exception as error:  # noqa: BLE001 - every failure means "not ready", never a crash
                self.last_error = f"{type(error).__name__}: {error}"[:500]
                log.error("champion model could not be loaded: %s", self.last_error)
                self._on_failure(failure_reason(error))
                return False
            self.current = loaded
            self.last_error = None
            self._on_success(loaded)
            log.info("serving %s version %s (run %s)", loaded.name, loaded.version, loaded.run_id)
            return True

    def retry_in_background(self, interval_seconds: float) -> None:
        if self.ready or interval_seconds <= 0:
            return

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                if self.try_load():
                    return

        threading.Thread(target=loop, name="champion-loader", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def wait_until_ready(holder: ModelHolder, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if holder.ready:
            return True
        time.sleep(0.1)
    return holder.ready
