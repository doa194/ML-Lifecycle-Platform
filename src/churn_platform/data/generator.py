"""Deterministic synthetic customer snapshots with a hidden, probabilistic churn outcome.

The same simulation serves two purposes:
  * `generate_dataset` builds the versioned training dataset (the "data warehouse extract")
    for the DVC pipeline.
  * `simulate_customers` is reused by the traffic simulator to create production customers,
    so production data follows exactly the same world as the training data.

Determinism: identical seed, row count, as-of date and config always give identical data,
because all randomness comes from one seeded NumPy generator used in a fixed order.

Label semantics: a snapshot's churn outcome covers the `label_window_days` after it. A
dataset extracted "as of" date D only contains snapshots whose outcome is already known on
D, i.e. snapshot_date <= D - label_window_days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from churn_platform.config import DataConfig, ProfileParams
from churn_platform.data.schema import CATEGORY_LEVELS, DATASET_COLUMNS, LABEL_COLUMN

# Relative usage and feature adoption per plan: premium customers use the product more.
_PLAN_USAGE_FACTOR = {"basic": 0.8, "standard": 1.0, "premium": 1.3}
_PLAN_ADOPTION_FACTOR = {"basic": 0.8, "standard": 1.0, "premium": 1.25}


@dataclass(frozen=True)
class SimulatedCustomers:
    features: pd.DataFrame  # feature columns only, one row per customer snapshot
    churn_probability: np.ndarray  # the hidden true probability (never shown to models)
    churned: np.ndarray  # sampled 0/1 outcome for the 30 days after the snapshot


def snapshot_window(as_of: date, config: DataConfig) -> tuple[date, date]:
    """First and last snapshot date whose outcome is known on `as_of`."""
    last = as_of - timedelta(days=config.generation.label_window_days)
    first = last - timedelta(days=config.generation.window_days - 1)
    return first, last


def generate_dataset(config: DataConfig, as_of: date, seed: int | None = None, rows: int | None = None) -> pd.DataFrame:
    """Build the training dataset extract: snapshots spread over the window ending at as_of."""
    seed = config.generation.seed if seed is None else seed
    rows = config.generation.rows if rows is None else rows
    rng = np.random.default_rng(seed)
    first, _ = snapshot_window(as_of, config)
    offsets = rng.integers(0, config.generation.window_days, size=rows)
    dates = np.array([first + timedelta(days=int(o)) for o in offsets])
    profiles = np.array([config.profile_for(d) for d in dates])
    simulated = simulate_customers(rng, profiles, config)

    frame = simulated.features
    frame.insert(0, "snapshot_date", pd.to_datetime(dates))
    frame.insert(0, "customer_id", [f"C{seed}-{i:06d}" for i in range(rows)])
    frame[LABEL_COLUMN] = simulated.churned
    return frame.sort_values(["snapshot_date", "customer_id"]).reset_index(drop=True)[list(DATASET_COLUMNS)]


def simulate_customers(rng: np.random.Generator, profile_names: np.ndarray, config: DataConfig) -> SimulatedCustomers:
    """Draw feature values and churn outcomes; each row follows its own profile."""
    n = len(profile_names)
    params = {name: config.profiles[name] for name in set(profile_names)}

    def per_row(getter) -> np.ndarray:
        values = {name: getter(p) for name, p in params.items()}
        return np.array([values[name] for name in profile_names], dtype=float)

    tenure = np.clip(rng.gamma(1.4, 16.0, n), 0, 180).astype(int)
    plan = _choose(rng, profile_names, params, lambda p: p.plan_mix, "plan_tier")
    contract = _choose(rng, profile_names, params, lambda p: p.contract_mix, "contract_type")
    # Brand-new customers rarely sit on multi-year contracts yet.
    contract = np.where((tenure < 3) & (contract == "two_year"), "monthly", contract)

    on_contract = contract != "monthly"
    autopay = rng.random(n) < np.clip(per_row(lambda p: p.autopay_rate) + 0.12 * on_contract, 0, 0.95)

    price_increase = rng.random(n) < per_row(lambda p: p.price_increase_rate)
    pct_low = per_row(lambda p: p.price_increase_pct[0])
    pct_high = per_row(lambda p: p.price_increase_pct[1])
    increase_pct = rng.uniform(pct_low, pct_high) * price_increase
    base_charge = np.array([params[name].base_charge[tier] for name, tier in zip(profile_names, plan, strict=True)])
    add_ons = (rng.random(n) < 0.3) * rng.choice([5.0, 10.0, 15.0], size=n)
    charge = base_charge * (1 + rng.normal(0, 0.06, n)) * (1 + increase_pct) + add_ons
    charge = np.round(np.clip(charge, 5, 400), 2)

    payment_failures = rng.poisson(per_row(lambda p: p.payment_failure_rate) * (1 + 1.5 * ~autopay))
    late_payments = rng.poisson(0.25 + 0.6 * ~autopay + 0.4 * payment_failures)

    usage_factor = np.array([_PLAN_USAGE_FACTOR[tier] for tier in plan])
    usage = rng.lognormal(np.log(per_row(lambda p: p.usage_hours_median) * usage_factor), 0.55)
    usage = np.round(np.clip(usage, 0, 400), 1)
    sessions = rng.poisson(1 + usage * 0.9)
    login_gap = rng.exponential(per_row(lambda p: p.login_gap_scale) * 90 / (sessions + 3))
    days_since_login = np.clip(np.round(login_gap), 0, 90).astype(int)
    usage_trend = np.round(
        np.clip(rng.normal(per_row(lambda p: p.usage_trend_mean), per_row(lambda p: p.usage_trend_sd)), -1, 1.5), 3
    )

    ticket_rate = per_row(lambda p: p.support_ticket_rate) * (1 + 0.6 * (payment_failures > 0) + 0.4 * price_increase)
    tickets = rng.poisson(ticket_rate)
    complaints = rng.binomial(tickets, per_row(lambda p: p.complaint_share))
    resolution = rng.lognormal(np.log(per_row(lambda p: p.resolution_hours_median)), 0.6)
    resolution = np.where(tickets > 0, np.round(np.clip(resolution, 0.5, 1500), 1), np.nan)

    adoption_factor = np.array([_PLAN_ADOPTION_FACTOR[tier] for tier in plan])
    adoption_p = per_row(lambda p: p.feature_adoption_rate) * adoption_factor * (0.7 + 0.3 * np.minimum(tenure, 24) / 24)
    features_adopted = rng.binomial(10, np.clip(adoption_p, 0, 1))

    features = pd.DataFrame(
        {
            "plan_tier": plan,
            "contract_type": contract,
            "autopay_enabled": autopay,
            "recent_price_increase": price_increase,
            "tenure_months": tenure,
            "monthly_charge": charge,
            "payment_failures_90d": payment_failures,
            "late_payments_12m": late_payments,
            "monthly_usage_hours": usage,
            "sessions_30d": sessions,
            "days_since_last_login": days_since_login,
            "usage_trend_pct": usage_trend,
            "support_tickets_90d": tickets,
            "complaints_90d": complaints,
            "avg_resolution_hours": resolution,
            "features_adopted": features_adopted,
        }
    )
    probability = true_churn_probability(features, profile_names, params)
    churned = (rng.random(n) < probability).astype(np.int8)
    return SimulatedCustomers(features=features, churn_probability=probability, churned=churned)


def true_churn_probability(features: pd.DataFrame, profile_names: np.ndarray, params: dict[str, ProfileParams]) -> np.ndarray:
    """The hidden logistic churn logic of the synthetic world (per-row profile coefficients)."""

    def coef(getter) -> np.ndarray:
        values = {name: getter(p.churn_model) for name, p in params.items()}
        return np.array([values[name] for name in profile_names], dtype=float)

    f = features
    new_customer_months = coef(lambda m: m.new_customer_months)
    contract_effect = np.array(
        [params[name].churn_model.contract[kind] for name, kind in zip(profile_names, f["contract_type"], strict=True)]
    )
    logit = (
        coef(lambda m: m.intercept)
        + coef(lambda m: m.new_customer) * (f["tenure_months"].to_numpy() < new_customer_months)
        + coef(lambda m: m.tenure_log) * np.log1p(f["tenure_months"].to_numpy())
        + contract_effect
        + coef(lambda m: m.recent_price_increase) * f["recent_price_increase"].to_numpy()
        + coef(lambda m: m.charge_per_10) * (f["monthly_charge"].to_numpy() - 40) / 10
        + coef(lambda m: m.autopay) * f["autopay_enabled"].to_numpy()
        + coef(lambda m: m.payment_failures) * f["payment_failures_90d"].to_numpy()
        + coef(lambda m: m.late_payments) * f["late_payments_12m"].to_numpy()
        + coef(lambda m: m.usage_log) * np.log1p(f["monthly_usage_hours"].to_numpy())
        + coef(lambda m: m.days_since_login_per_10) * f["days_since_last_login"].to_numpy() / 10
        + coef(lambda m: m.usage_trend) * f["usage_trend_pct"].to_numpy()
        + coef(lambda m: m.complaints) * f["complaints_90d"].to_numpy()
        + coef(lambda m: m.resolution_per_24h) * np.nan_to_num(f["avg_resolution_hours"].to_numpy(), nan=0.0) / 24
        + coef(lambda m: m.features_adopted) * f["features_adopted"].to_numpy()
    )
    return 1.0 / (1.0 + np.exp(-logit))


def _choose(rng: np.random.Generator, profile_names: np.ndarray, params: dict[str, ProfileParams], mix_of, column: str) -> np.ndarray:
    """Draw a categorical value per row from that row's profile mix (fixed level order)."""
    levels = CATEGORY_LEVELS[column]
    cumulative = {
        name: np.cumsum([mix_of(p).get(level, 0.0) for level in levels]) for name, p in params.items()
    }
    draws = rng.random(len(profile_names))
    indices = np.array(
        [min(int(np.searchsorted(cumulative[name], u, side="right")), len(levels) - 1) for name, u in zip(profile_names, draws, strict=True)]
    )
    return np.array(levels, dtype=object)[indices]
