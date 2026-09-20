"""Small, deterministic datasets for unit tests (built with the real generator and config)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

from churn_platform.config import DataConfig, load_data_config
from churn_platform.data.generator import generate_dataset
from churn_platform.data.preparation import prepare_dataset

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIG = REPO_ROOT / "config"
AS_OF = date(2026, 1, 1)


@lru_cache(maxsize=1)
def data_config() -> DataConfig:
    return load_data_config(REPO_CONFIG)


def small_dataset(rows: int = 3000, seed: int = 7, as_of: date = AS_OF) -> pd.DataFrame:
    return prepare_dataset(generate_dataset(data_config(), as_of, seed=seed, rows=rows))
