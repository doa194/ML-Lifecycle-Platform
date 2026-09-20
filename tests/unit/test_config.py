"""Configuration validation: invalid policy is rejected when it is loaded, not when it is used."""

from __future__ import annotations

import shutil

import pytest
import yaml
from pydantic import ValidationError

from churn_platform.config import load_data_config
from tests.support.data import REPO_CONFIG, data_config


def test_profiles_inherit_baseline_and_override_only_what_they_declare():
    baseline = data_config().profiles["baseline"]
    pricing = data_config().profiles["pricing_shift"]

    assert pricing.churn_model.recent_price_increase > baseline.churn_model.recent_price_increase
    assert pricing.churn_model.usage_trend == baseline.churn_model.usage_trend
    assert pricing.usage_hours_median == baseline.usage_hours_median


def copy_config_with(tmp_path, file_name, change):
    shutil.copytree(REPO_CONFIG, tmp_path, dirs_exist_ok=True)
    raw = yaml.safe_load((tmp_path / file_name).read_text(encoding="utf-8"))
    change(raw)
    (tmp_path / file_name).write_text(yaml.safe_dump(raw), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda raw: raw["split"].update(purge_days=10), "purge_days"),
        (lambda raw: raw["timeline"].append({"start": "2027-01-01", "profile": "unknown"}), "unknown profiles"),
        (lambda raw: raw["profiles"]["baseline"]["plan_mix"].update(basic=0.9), "sum to 1"),
        (lambda raw: raw["generation"].update(unexpected_key=1), "unexpected_key"),
    ],
)
def test_invalid_data_config_is_rejected(tmp_path, change, message):
    config_dir = copy_config_with(tmp_path, "data.yaml", change)

    with pytest.raises(ValidationError, match=message):
        load_data_config(config_dir)
