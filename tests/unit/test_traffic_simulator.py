"""Traffic simulator: deterministic, follows the world timeline, and speaks the API contract."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from churn_platform.serving.schemas import PredictionRequest
from churn_platform.simulation.traffic import build_requests
from tests.support.data import data_config


def test_same_seed_replays_the_same_customers():
    first = build_requests(data_config(), date(2026, 1, 1), date(2026, 3, 31), 50, seed=3)
    second = build_requests(data_config(), date(2026, 1, 1), date(2026, 3, 31), 50, seed=3)

    assert [r.payload for r in first] == [r.payload for r in second]


def test_snapshot_dates_stay_in_the_window_and_follow_the_timeline():
    requests = build_requests(data_config(), date(2025, 12, 15), date(2026, 1, 15), 300, seed=4)

    dates = [date.fromisoformat(r.payload["snapshot_date"]) for r in requests]
    assert min(dates) >= date(2025, 12, 15) and max(dates) <= date(2026, 1, 15)
    assert {r.profile for r, d in zip(requests, dates, strict=True) if d < date(2026, 1, 1)} == {"baseline"}
    assert {r.profile for r, d in zip(requests, dates, strict=True) if d >= date(2026, 1, 1)} == {"pricing_shift"}


def test_every_valid_simulated_request_satisfies_the_api_contract():
    requests = build_requests(data_config(), date(2026, 1, 1), date(2026, 12, 31), 300, seed=5, invalid_share=0.1)

    for request in requests:
        if request.valid:
            PredictionRequest.model_validate(request.payload)
        else:
            with pytest.raises(ValidationError):
                PredictionRequest.model_validate(request.payload)
    assert any(not r.valid for r in requests)


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="unknown profile"):
        build_requests(data_config(), date(2026, 1, 1), date(2026, 1, 31), 10, seed=1, profile="holiday_rush")
