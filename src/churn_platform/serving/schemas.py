"""Request and response contract of the inference API.

Field limits come from `churn_platform.data.schema`, the same definitions the training data
is validated against, so the API accepts exactly the value space the model was trained on.
Invalid requests are rejected with HTTP 422 before they reach the model:
  * unknown fields are refused (`extra="forbid"`), which also blocks outcome-leaking fields
  * booleans and counts must be real JSON booleans / integers (no "yes" or "3.5")
  * domain rules match training validation (complaints <= tickets, resolution time only
    when there were tickets)
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from churn_platform.data.schema import FEATURE_COLUMNS, NUMERIC_RANGES


def _count(name: str):
    low, high = NUMERIC_RANGES[name]
    return Annotated[int, Field(strict=True, ge=int(low), le=int(high))]


def _amount(name: str):
    low, high = NUMERIC_RANGES[name]
    return Annotated[float, Field(strict=True, ge=low, le=high, allow_inf_nan=False)]


StrictBool = Annotated[bool, Field(strict=True)]


class CustomerFeatures(BaseModel):
    """Everything known about the customer on the snapshot date. No outcome information."""

    model_config = ConfigDict(extra="forbid")

    plan_tier: Literal["basic", "standard", "premium"]
    contract_type: Literal["monthly", "annual", "two_year"]
    autopay_enabled: StrictBool
    recent_price_increase: StrictBool = Field(description="price increase in the last 60 days")
    tenure_months: _count("tenure_months")
    monthly_charge: _amount("monthly_charge")
    payment_failures_90d: _count("payment_failures_90d")
    late_payments_12m: _count("late_payments_12m")
    monthly_usage_hours: _amount("monthly_usage_hours")
    sessions_30d: _count("sessions_30d")
    days_since_last_login: _count("days_since_last_login")
    usage_trend_pct: _amount("usage_trend_pct") = Field(description="usage change vs. previous period, -0.2 = -20%")
    support_tickets_90d: _count("support_tickets_90d")
    complaints_90d: _count("complaints_90d")
    avg_resolution_hours: _amount("avg_resolution_hours") | None = Field(
        default=None, description="null when the customer had no support tickets"
    )
    features_adopted: _count("features_adopted")

    @model_validator(mode="after")
    def _consistent(self) -> CustomerFeatures:
        if self.complaints_90d > self.support_tickets_90d:
            raise ValueError("complaints_90d cannot exceed support_tickets_90d")
        if (self.avg_resolution_hours is None) != (self.support_tickets_90d == 0):
            raise ValueError("avg_resolution_hours must be given exactly when support_tickets_90d > 0")
        return self


class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    snapshot_date: date | None = Field(default=None, description="date the features describe; defaults to today (UTC)")
    features: CustomerFeatures


class PredictionResponse(BaseModel):
    prediction_id: UUID
    customer_id: str
    snapshot_date: date
    churn_prediction: bool
    churn_probability: float
    risk_level: Literal["low", "medium", "high"]
    decision_threshold: float
    model_name: str
    model_version: str


def to_frame(features: CustomerFeatures) -> pd.DataFrame:
    """One-row frame in the exact column layout the model pipeline expects."""
    row = features.model_dump()
    return pd.DataFrame([{column: row[column] for column in FEATURE_COLUMNS}])
