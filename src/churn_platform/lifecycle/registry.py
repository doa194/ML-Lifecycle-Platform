"""Thin adapter over the MLflow Model Registry.

All registry reads return `VersionState` objects for the pure lifecycle rules, and all
registry writes (versions, aliases, tags) happen here. Only the lifecycle workflows
(registration, gate, promotion, rollback) use the write methods; training, serving and
monitoring never change registry state.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

from churn_platform.lifecycle.states import Alias, VersionState


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ModelRegistry:
    def __init__(self, client: MlflowClient, model_name: str):
        self.client = client
        self.model_name = model_name

    # ------------------------------------------------------------------ reads

    def aliases(self) -> dict[str, str]:
        """alias -> version for the registered model ({} if the model does not exist yet)."""
        try:
            return dict(self.client.get_registered_model(self.model_name).aliases or {})
        except MlflowException as error:
            if error.error_code == "RESOURCE_DOES_NOT_EXIST":
                return {}
            raise

    def version_state(self, version: str, aliases: dict[str, str] | None = None) -> VersionState:
        aliases = self.aliases() if aliases is None else aliases
        model_version = self.client.get_model_version(self.model_name, str(version))
        return VersionState(
            version=str(model_version.version),
            aliases=frozenset(alias for alias, v in aliases.items() if str(v) == str(model_version.version)),
            tags=dict(model_version.tags or {}),
        )

    def alias_state(self, alias: Alias) -> VersionState | None:
        aliases = self.aliases()
        version = aliases.get(alias)
        return self.version_state(version, aliases) if version is not None else None

    def all_versions(self) -> list[VersionState]:
        aliases = self.aliases()
        if not aliases and not self._exists():
            return []
        versions = self.client.search_model_versions(f"name='{self.model_name}'")
        return sorted((self.version_state(v.version, aliases) for v in versions), key=lambda s: int(s.version))

    def find_version_by_run(self, run_id: str) -> VersionState | None:
        if not self._exists():
            return None
        matches = self.client.search_model_versions(f"name='{self.model_name}' and run_id='{run_id}'")
        return self.version_state(min(matches, key=lambda v: int(v.version)).version) if matches else None

    def run_id(self, version: str) -> str:
        return self.client.get_model_version(self.model_name, str(version)).run_id

    def model_uri(self, version: str) -> str:
        return f"models:/{self.model_name}/{version}"

    # ----------------------------------------------------------------- writes

    def ensure_registered_model(self) -> None:
        if not self._exists():
            self.client.create_registered_model(
                self.model_name,
                description=(
                    "Predicts whether an active subscription customer voluntarily churns within "
                    "30 days of a snapshot. Aliases: candidate, challenger, champion."
                ),
            )

    def create_version(self, source_uri: str, run_id: str, tags: dict[str, str], timeout_s: int = 120) -> str:
        import mlflow

        model_version = mlflow.register_model(source_uri, self.model_name, tags=tags)
        deadline = time.monotonic() + timeout_s
        while self.client.get_model_version(self.model_name, model_version.version).status != "READY":
            if time.monotonic() > deadline:
                raise TimeoutError(f"model version {model_version.version} not READY after {timeout_s}s")
            time.sleep(1)
        if model_version.run_id != run_id:
            raise RuntimeError(f"registered version points to run {model_version.run_id}, expected {run_id}")
        return str(model_version.version)

    def set_alias(self, alias: Alias, version: str) -> None:
        self.client.set_registered_model_alias(self.model_name, alias, str(version))

    def delete_alias(self, alias: Alias) -> None:
        try:
            self.client.delete_registered_model_alias(self.model_name, alias)
        except MlflowException as error:
            if error.error_code not in ("RESOURCE_DOES_NOT_EXIST", "INVALID_PARAMETER_VALUE"):
                raise

    def set_tags(self, version: str, tags: dict[str, str]) -> None:
        for key, value in tags.items():
            self.client.set_model_version_tag(self.model_name, str(version), key, str(value))

    def _exists(self) -> bool:
        try:
            self.client.get_registered_model(self.model_name)
            return True
        except MlflowException as error:
            if error.error_code == "RESOURCE_DOES_NOT_EXIST":
                return False
            raise
