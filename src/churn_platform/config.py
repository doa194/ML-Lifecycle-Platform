"""Typed loading of the policy configuration files in `config/`.

Every threshold, window and algorithm setting lives in YAML so that policy is explicit and
reviewable instead of scattered through code. Each file is parsed into a strict Pydantic
model: unknown keys, missing values and invalid combinations fail at load time, long
before a pipeline stage or a promotion decision uses them.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------- data.yaml


class ChurnModelParams(StrictModel):
    """Coefficients of the hidden churn logic used by the synthetic world (log-odds units)."""

    intercept: float
    new_customer_months: int = Field(gt=0)
    new_customer: float
    tenure_log: float
    contract: dict[str, float]
    recent_price_increase: float
    charge_per_10: float
    autopay: float
    payment_failures: float
    late_payments: float
    usage_log: float
    days_since_login_per_10: float
    usage_trend: float
    complaints: float
    resolution_per_24h: float
    features_adopted: float


class ProfileParams(StrictModel):
    plan_mix: dict[str, float]
    contract_mix: dict[str, float]
    base_charge: dict[str, float]
    price_increase_rate: float = Field(ge=0, le=1)
    price_increase_pct: tuple[float, float]
    autopay_rate: float = Field(ge=0, le=1)
    payment_failure_rate: float = Field(ge=0)
    usage_hours_median: float = Field(gt=0)
    usage_trend_mean: float
    usage_trend_sd: float = Field(gt=0)
    login_gap_scale: float = Field(gt=0)
    support_ticket_rate: float = Field(ge=0)
    complaint_share: float = Field(ge=0, le=1)
    resolution_hours_median: float = Field(gt=0)
    feature_adoption_rate: float = Field(ge=0, le=1)
    churn_model: ChurnModelParams

    @field_validator("plan_mix", "contract_mix")
    @classmethod
    def _mix_sums_to_one(cls, mix: dict[str, float]) -> dict[str, float]:
        if abs(sum(mix.values()) - 1.0) > 1e-6:
            raise ValueError(f"probabilities must sum to 1, got {sum(mix.values())}")
        return mix


class GenerationConfig(StrictModel):
    seed: int
    rows: int = Field(gt=0)
    window_days: int = Field(gt=0)
    label_window_days: int = Field(gt=0)


class TimelineEntry(StrictModel):
    start: date
    profile: str


class SplitConfig(StrictModel):
    validation_days: int = Field(gt=0)
    test_days: int = Field(gt=0)
    purge_days: int = Field(ge=0)
    min_rows_per_split: int = Field(gt=0)


class DataValidationConfig(StrictModel):
    min_rows: int = Field(gt=0)
    churn_rate_min: float = Field(ge=0, le=1)
    churn_rate_max: float = Field(ge=0, le=1)


class DataConfig(StrictModel):
    generation: GenerationConfig
    timeline: list[TimelineEntry]
    profiles: dict[str, ProfileParams]
    split: SplitConfig
    validation: DataValidationConfig

    @model_validator(mode="after")
    def _check_consistency(self) -> DataConfig:
        if not self.timeline:
            raise ValueError("timeline needs at least one entry")
        starts = [entry.start for entry in self.timeline]
        if starts != sorted(starts):
            raise ValueError("timeline entries must be in chronological order")
        unknown = {entry.profile for entry in self.timeline} - self.profiles.keys()
        if unknown:
            raise ValueError(f"timeline references unknown profiles: {sorted(unknown)}")
        # A purge shorter than the outcome window would let training labels observe the
        # validation period: exactly the temporal leakage the split exists to prevent.
        if self.split.purge_days < self.generation.label_window_days:
            raise ValueError("split.purge_days must be >= generation.label_window_days")
        return self

    def profile_for(self, day: date) -> str:
        active = self.timeline[0].profile
        for entry in self.timeline:
            if entry.start <= day:
                active = entry.profile
        return active


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_profiles(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Expand `extends:` so every profile is a complete parameter set."""
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(name: str, seen: tuple[str, ...] = ()) -> dict[str, Any]:
        if name in seen:
            raise ValueError(f"circular profile inheritance: {' -> '.join((*seen, name))}")
        if name in resolved:
            return resolved[name]
        spec = dict(raw[name])
        parent = spec.pop("extends", None)
        full = _deep_merge(resolve(parent, (*seen, name)), spec) if parent else spec
        resolved[name] = full
        return full

    for profile_name in raw:
        resolve(profile_name)
    return resolved


# ------------------------------------------------------------------------ training.yaml


class ThresholdSearch(StrictModel):
    min: float = Field(gt=0, lt=1)
    max: float = Field(gt=0, lt=1)
    step: float = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def _ordered(self) -> ThresholdSearch:
        if self.min >= self.max:
            raise ValueError("threshold_search.min must be below max")
        return self


class EvaluationConfig(StrictModel):
    segment_columns: list[str]
    min_segment_rows: int = Field(gt=0)
    latency_samples: int = Field(gt=0)


class ReferenceConfig(StrictModel):
    max_rows: int = Field(gt=0)


SUPPORTED_ALGORITHMS = ("logistic_regression", "random_forest", "hist_gradient_boosting")


class TrainingConfig(StrictModel):
    random_state: int
    selection_metric: Literal["f1", "roc_auc", "recall"]
    threshold_search: ThresholdSearch
    new_customer_months: int = Field(gt=0)
    algorithms: dict[str, dict[str, Any]]
    evaluation: EvaluationConfig
    reference: ReferenceConfig

    @field_validator("algorithms")
    @classmethod
    def _known_algorithms(cls, algorithms: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        unknown = set(algorithms) - set(SUPPORTED_ALGORITHMS)
        if unknown or not algorithms:
            raise ValueError(f"algorithms must be a non-empty subset of {SUPPORTED_ALGORITHMS}; unknown: {sorted(unknown)}")
        return algorithms


# -------------------------------------------------------------------- quality_gate.yaml


class AbsoluteThresholds(StrictModel):
    f1_min: float = Field(ge=0, le=1)
    recall_min: float = Field(ge=0, le=1)
    roc_auc_min: float = Field(ge=0, le=1)


class SegmentPolicy(StrictModel):
    columns: list[str]
    recall_min: float = Field(ge=0, le=1)
    max_f1_drop_vs_champion: float = Field(ge=0, le=1)


class ChampionComparison(StrictModel):
    min_f1_delta: float = Field(ge=-1, le=1)
    min_roc_auc_delta: float = Field(ge=-1, le=1)


class OperationalLimits(StrictModel):
    max_p95_latency_ms: float = Field(gt=0)


class ApprovalPolicy(StrictModel):
    manual_approval_required: bool


class QualityGatePolicy(StrictModel):
    absolute: AbsoluteThresholds
    segments: SegmentPolicy
    champion_comparison: ChampionComparison
    operational: OperationalLimits
    approval: ApprovalPolicy


# --------------------------------------------------------------------------- loading


def read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_data_config(config_dir: Path) -> DataConfig:
    raw = read_yaml(config_dir / "data.yaml")
    raw["profiles"] = _resolve_profiles(raw.get("profiles", {}))
    return DataConfig.model_validate(raw)


def load_training_config(config_dir: Path) -> TrainingConfig:
    return TrainingConfig.model_validate(read_yaml(config_dir / "training.yaml"))


class RiskLevels(StrictModel):
    medium_ratio: float = Field(gt=0, lt=1)


class ObservationPolicy(StrictModel):
    on_persistence_failure: Literal["reject", "serve"]


class ModelLoading(StrictModel):
    retry_interval_seconds: float = Field(ge=0)


class ServingConfig(StrictModel):
    risk_levels: RiskLevels
    observations: ObservationPolicy
    model_loading: ModelLoading
    max_request_bytes: int = Field(gt=0)


def load_serving_config(config_dir: Path) -> ServingConfig:
    return ServingConfig.model_validate(read_yaml(config_dir / "serving.yaml"))


# ------------------------------------------------------- monitoring.yaml / retraining.yaml


class MonitoringWindow(StrictModel):
    max_observations: int = Field(gt=0)
    min_observations: int = Field(gt=0)


class DriftConfig(StrictModel):
    numerical_method: Literal["wasserstein"]
    categorical_method: Literal["jensenshannon"]
    feature_threshold: float = Field(gt=0, lt=1)
    dataset_drift_share: float = Field(gt=0, le=1)
    prediction_threshold: float = Field(gt=0, lt=1)


class DataQualityConfig(StrictModel):
    max_missing_share_increase: float = Field(ge=0, le=1)
    max_duplicate_share: float = Field(ge=0, le=1)


class PerformanceConfig(StrictModel):
    min_labeled: int = Field(gt=0)
    max_labeled: int = Field(gt=0)


class WorkerConfig(StrictModel):
    interval_seconds: float = Field(gt=0)


class MonitoringConfig(StrictModel):
    window: MonitoringWindow
    drift: DriftConfig
    data_quality: DataQualityConfig
    performance: PerformanceConfig
    worker: WorkerConfig


class RetrainingPolicy(StrictModel):
    min_observations: int = Field(gt=0)
    severe_drift_share: float = Field(gt=0, le=1)
    require_prediction_drift: bool
    max_f1_drop: float = Field(ge=0, le=1)
    max_recall_drop: float = Field(ge=0, le=1)
    block_on_data_quality_issues: bool
    cooldown_hours: float = Field(ge=0)


class ControllerConfig(StrictModel):
    auto_promote: bool
    stale_after_minutes: float = Field(gt=0)
    push_data: bool


class RetrainingConfig(StrictModel):
    policy: RetrainingPolicy
    controller: ControllerConfig


def load_monitoring_config(config_dir: Path) -> MonitoringConfig:
    return MonitoringConfig.model_validate(read_yaml(config_dir / "monitoring.yaml"))


def load_retraining_config(config_dir: Path) -> RetrainingConfig:
    return RetrainingConfig.model_validate(read_yaml(config_dir / "retraining.yaml"))


def load_quality_gate_policy(config_dir: Path) -> QualityGatePolicy:
    return QualityGatePolicy.model_validate(read_yaml(config_dir / "quality_gate.yaml"))


class DatasetParams(StrictModel):
    as_of: date


def load_dataset_params(workspace: Path) -> DatasetParams:
    """Read `params.yaml`: the pipeline run parameters that change between dataset versions."""
    return DatasetParams.model_validate(read_yaml(workspace / "params.yaml")["dataset"])

