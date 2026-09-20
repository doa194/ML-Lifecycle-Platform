"""The customer-snapshot schema: the single source of truth for columns and allowed values.

Training data validation, the preprocessing pipeline and the inference API request model
all read these definitions, so the model can never be trained on a column the API does not
accept (or the other way round).

Snapshot semantics: every feature must be knowable on `snapshot_date`. The label
`churned_30d` describes the 30 days *after* the snapshot, so anything that is only known
once a customer has started to leave (cancellation requests, closure dates, exit surveys...)
is outcome leakage and is rejected wherever a dataset enters the platform.
"""

from __future__ import annotations

from collections.abc import Iterable

# Bump when feature names, meanings or allowed values change; recorded with every model.
FEATURE_SCHEMA_VERSION = "1.0"

ID_COLUMNS = ("customer_id", "snapshot_date")
LABEL_COLUMN = "churned_30d"

CATEGORY_LEVELS: dict[str, tuple[str, ...]] = {
    "plan_tier": ("basic", "standard", "premium"),
    "contract_type": ("monthly", "annual", "two_year"),
}
CATEGORICAL_FEATURES = tuple(CATEGORY_LEVELS)
BOOLEAN_FEATURES = ("autopay_enabled", "recent_price_increase")

# Inclusive (min, max) domain limits. They reject impossible values, not unusual ones:
# drift detection, not validation, is responsible for "unusual".
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "tenure_months": (0, 240),
    "monthly_charge": (1, 1000),
    "payment_failures_90d": (0, 30),
    "late_payments_12m": (0, 24),
    "monthly_usage_hours": (0, 744),
    "sessions_30d": (0, 3000),
    "days_since_last_login": (0, 365),
    "usage_trend_pct": (-1, 5),
    "support_tickets_90d": (0, 100),
    "complaints_90d": (0, 100),
    "avg_resolution_hours": (0, 2000),
    "features_adopted": (0, 10),
}
INTEGER_FEATURES = (
    "tenure_months",
    "payment_failures_90d",
    "late_payments_12m",
    "sessions_30d",
    "days_since_last_login",
    "support_tickets_90d",
    "complaints_90d",
    "features_adopted",
)
NUMERIC_FEATURES = tuple(NUMERIC_RANGES)

# Average resolution time does not exist for customers without support tickets.
NULLABLE_FEATURES = frozenset({"avg_resolution_hours"})

FEATURE_COLUMNS = CATEGORICAL_FEATURES + BOOLEAN_FEATURES + NUMERIC_FEATURES
DATASET_COLUMNS = ID_COLUMNS + FEATURE_COLUMNS + (LABEL_COLUMN,)

# Known outcome-leaking fields from typical CRM/billing exports, plus name fragments that
# indicate post-snapshot information. Matching is case-insensitive.
PROHIBITED_COLUMNS = frozenset(
    {
        "cancellation_requested",
        "cancellation_request_date",
        "cancellation_status",
        "cancellation_date",
        "cancel_reason",
        "churn_date",
        "churned",
        "account_closed",
        "account_closed_date",
        "exit_survey_score",
        "retention_offer_accepted",
        "final_invoice_amount",
        "days_until_churn",
        "win_back_campaign",
    }
)
PROHIBITED_FRAGMENTS = ("cancel", "churn", "closed", "exit_survey", "retention_offer", "final_invoice", "win_back")


def find_leakage_columns(columns: Iterable[str]) -> list[str]:
    """Return columns that carry post-snapshot (outcome) information. The label is allowed."""
    leaking = []
    for column in columns:
        name = column.lower()
        if name == LABEL_COLUMN:
            continue
        if name in PROHIBITED_COLUMNS or any(fragment in name for fragment in PROHIBITED_FRAGMENTS):
            leaking.append(column)
    return leaking
