"""Model-quality smoke test and artifact safety for every supported algorithm.

The smoke test proves the pipeline learns real signal from the synthetic data (clearly
better than predicting "churn" for everyone). The artifact tests prove every algorithm
survives the skops save/load round trip with the configured trusted-type list, and that the
integrity fingerprint reacts to any change in the model files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
import skops.io as sio

from churn_platform.config import load_training_config
from churn_platform.data.schema import FEATURE_COLUMNS, LABEL_COLUMN
from churn_platform.data.splitting import time_split
from churn_platform.features.preprocessing import TRUSTED_MODEL_TYPES
from churn_platform.tracking.model_io import compute_fingerprint
from churn_platform.training.cycle import fit_candidate
from churn_platform.training.metrics import classification_metrics, trivial_baseline
from tests.support.data import REPO_CONFIG, data_config, small_dataset


@pytest.fixture(scope="module")
def split():
    return time_split(small_dataset(rows=9000, seed=99), data_config().split)


@pytest.fixture(scope="module")
def training_config():
    return load_training_config(REPO_CONFIG)


@pytest.mark.parametrize("algorithm", ["logistic_regression", "random_forest", "hist_gradient_boosting"])
def test_every_algorithm_learns_signal_beyond_the_trivial_baseline(algorithm, split, training_config):
    result = fit_candidate(algorithm, training_config, split.train, split.validation)
    probability = result.pipeline.predict_proba(split.test.loc[:, list(FEATURE_COLUMNS)])[:, 1]

    test = classification_metrics(split.test[LABEL_COLUMN], probability, result.decision_threshold)
    baseline = trivial_baseline(split.test[LABEL_COLUMN])

    assert test["roc_auc"] > 0.75
    assert test["f1"] > baseline["f1"] + 0.1


@pytest.mark.parametrize("algorithm", ["logistic_regression", "random_forest", "hist_gradient_boosting"])
def test_skops_round_trip_keeps_predictions_unchanged(algorithm, split, training_config):
    pipeline = fit_candidate(algorithm, training_config, split.train.head(2000), split.validation.head(600)).pipeline

    restored = sio.loads(sio.dumps(pipeline), trusted=TRUSTED_MODEL_TYPES)

    features = split.test.loc[:, list(FEATURE_COLUMNS)]
    # Parallel tree averaging may differ in the last bit; anything larger would be a real change.
    np.testing.assert_allclose(pipeline.predict_proba(features), restored.predict_proba(features), rtol=0, atol=1e-12)


def write_model_dir(root: Path, files: dict[str, bytes]) -> Path:
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    return root


def test_fingerprint_detects_any_changed_file(tmp_path):
    original = write_model_dir(tmp_path / "a", {"MLmodel": b"meta", "model.skops": b"weights"})
    tampered = write_model_dir(tmp_path / "b", {"MLmodel": b"meta", "model.skops": b"weightz"})

    assert compute_fingerprint(original) != compute_fingerprint(tampered)


def test_fingerprint_is_identical_on_every_operating_system(tmp_path):
    # Models are trained on the host (Windows here) and verified in Linux containers, so the
    # file order must not depend on how the OS compares mixed-case names.
    model_dir = write_model_dir(tmp_path / "m", {"MLmodel": b"meta", "conda.yaml": b"env", "model.skops": b"w"})
    expected = hashlib.sha256()
    for name in ("MLmodel", "conda.yaml", "model.skops"):  # plain string (byte) order
        expected.update(name.encode())
        expected.update(hashlib.sha256((model_dir / name).read_bytes()).digest())

    assert compute_fingerprint(model_dir) == expected.hexdigest()


def test_fingerprint_ignores_download_only_registry_metadata(tmp_path):
    trained = write_model_dir(tmp_path / "a", {"MLmodel": b"meta", "model.skops": b"weights"})
    downloaded = write_model_dir(
        tmp_path / "b", {"MLmodel": b"meta", "model.skops": b"weights", "registered_model_meta": b"name: x"}
    )

    assert compute_fingerprint(trained) == compute_fingerprint(downloaded)
