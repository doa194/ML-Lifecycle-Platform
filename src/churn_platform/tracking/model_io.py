"""Saving and loading the deployable model, with integrity verification.

Every model is logged to MLflow in the skops format (a safe format that refuses to load
unexpected object types, unlike pickle) and is fingerprinted with SHA-256 right after
upload. Every later load - evaluation, quality gate, inference service, monitoring - goes
through `load_model`, which recomputes the fingerprint and refuses a model whose files
changed after training. The decision threshold travels inside the artifact metadata, so
each consumer applies the threshold the model was validated with.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from churn_platform.data.schema import (
    FEATURE_COLUMNS,
    FEATURE_SCHEMA_VERSION,
    NUMERIC_FEATURES,
)
from churn_platform.features.preprocessing import TRUSTED_MODEL_TYPES

# MLflow adds this file when a model is downloaded through a registry URI; it is not part
# of the trained artifact, so it is excluded from the fingerprint.
_DOWNLOAD_ONLY_FILES = {"registered_model_meta"}


class ModelIntegrityError(RuntimeError):
    """The model files do not match the fingerprint recorded when the model was trained."""


@dataclass
class LoadedModel:
    pipeline: Any
    decision_threshold: float
    fingerprint: str
    model_uri: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        return self.pipeline.predict_proba(features.loc[:, list(FEATURE_COLUMNS)])[:, 1]

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        return (self.predict_proba(features) >= self.decision_threshold).astype(int)


def compute_fingerprint(model_dir: Path) -> str:
    """SHA-256 over every file (relative path + content hash).

    Files are ordered by their relative POSIX path as a plain string. Sorting Path objects
    would be case-insensitive on Windows but case-sensitive on Linux ("MLmodel" vs
    "conda.yaml"), and a model trained on one OS must verify on the other.
    """
    root = Path(model_dir)
    files = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path.name not in _DOWNLOAD_ONLY_FILES
    }
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(relative.encode())
        digest.update(hashlib.sha256(files[relative].read_bytes()).digest())
    return digest.hexdigest()


def log_model(pipeline, decision_threshold: float, algorithm: str, input_example: pd.DataFrame):
    """Log the trained pipeline to the active MLflow run; returns (ModelInfo, fingerprint)."""
    import mlflow
    import sklearn
    import skops
    from mlflow.models import infer_signature

    # Numbers arrive as JSON numbers at inference time, so the signature declares them as
    # floats (integer columns could not represent a missing value).
    features = input_example.loc[:, list(FEATURE_COLUMNS)].astype({c: "float64" for c in NUMERIC_FEATURES})
    signature = infer_signature(features, pipeline.predict_proba(features)[:, 1])
    info = mlflow.sklearn.log_model(
        sk_model=pipeline,
        name="model",
        serialization_format="skops",
        skops_trusted_types=TRUSTED_MODEL_TYPES,
        signature=signature,
        input_example=features.head(3),
        # Explicit requirements avoid MLflow's slow automatic dependency inference.
        pip_requirements=[f"scikit-learn=={sklearn.__version__}", f"skops=={skops.__version__}"],
        metadata={
            "decision_threshold": decision_threshold,
            "algorithm": algorithm,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        local = mlflow.artifacts.download_artifacts(artifact_uri=info.model_uri, dst_path=tmp)
        fingerprint = compute_fingerprint(Path(local))
    return info, fingerprint


def load_model(model_uri: str, expected_fingerprint: str | None) -> LoadedModel:
    """Download, verify and load a model. Raises ModelIntegrityError on a mismatch.

    `expected_fingerprint=None` skips verification; production callers always pass it.
    """
    import mlflow
    from mlflow.models import Model

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(mlflow.artifacts.download_artifacts(artifact_uri=model_uri, dst_path=tmp))
        fingerprint = compute_fingerprint(local)
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            raise ModelIntegrityError(
                f"model at {model_uri} has fingerprint {fingerprint[:12]}..., expected {expected_fingerprint[:12]}..."
            )
        metadata = dict(Model.load(str(local)).metadata or {})
        pipeline = mlflow.sklearn.load_model(str(local))
    if "decision_threshold" not in metadata:
        raise ModelIntegrityError(f"model at {model_uri} has no decision_threshold metadata")
    return LoadedModel(
        pipeline=pipeline,
        decision_threshold=float(metadata["decision_threshold"]),
        fingerprint=fingerprint,
        model_uri=model_uri,
        metadata=metadata,
    )
