"""The three candidate algorithms of every training cycle, built from `config/training.yaml`."""

from __future__ import annotations

from typing import Any

from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression

_FACTORIES = {
    "logistic_regression": LogisticRegression,
    "random_forest": RandomForestClassifier,
    "hist_gradient_boosting": HistGradientBoostingClassifier,
}


def build_classifier(algorithm: str, params: dict[str, Any], random_state: int):
    if algorithm not in _FACTORIES:
        raise ValueError(f"unknown algorithm {algorithm!r}; expected one of {sorted(_FACTORIES)}")
    # A fixed random_state makes every run with the same data and config reproducible.
    return _FACTORIES[algorithm](random_state=random_state, **params)
