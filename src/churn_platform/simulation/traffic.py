"""Production traffic simulator: sends realistic customer snapshots to the inference API.

Customers are drawn from the same simulated world as the training data (same generator,
same timeline of distribution profiles), so "production" drifts exactly when the timeline
says the world changed. Each customer's true 30-day outcome is decided at the same time but
kept hidden in `simulation.customer_outcomes`; the delayed ground-truth workflow reveals it
later, like a billing system reporting cancellations weeks after the prediction.
"""

from __future__ import annotations

import logging
import math
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx
import numpy as np

from churn_platform.config import DataConfig
from churn_platform.data.generator import simulate_customers
from churn_platform.data.schema import FEATURE_COLUMNS
from churn_platform.storage.db import connect

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SimulatedRequest:
    payload: dict
    profile: str
    churned: int
    valid: bool


@dataclass
class TrafficReport:
    sent: int = 0
    status_counts: Counter = field(default_factory=Counter)
    predicted_churn: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    profiles: Counter = field(default_factory=Counter)

    def summary(self) -> dict:
        served = self.status_counts.get(200, 0)
        return {
            "sent": self.sent,
            "status_counts": dict(self.status_counts),
            "profiles": dict(self.profiles),
            "predicted_churn_share": round(self.predicted_churn / served, 3) if served else None,
            "p95_latency_ms": round(float(np.percentile(self.latencies_ms, 95)), 1) if self.latencies_ms else None,
        }


def default_seed(start: date, end: date, profile: str | None) -> int:
    """Same window + profile -> same customers (replayable); different windows never collide."""
    return zlib.crc32(f"{start}|{end}|{profile or 'timeline'}".encode()) % 1_000_000


def _json_value(value):
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if math.isnan(float(value)) else round(float(value), 4)
    return value


def build_requests(
    config: DataConfig,
    start: date,
    end: date,
    count: int,
    seed: int,
    profile: str | None = None,
    invalid_share: float = 0.0,
) -> list[SimulatedRequest]:
    """Deterministically create `count` customer snapshots dated between start and end."""
    if end < start:
        raise ValueError("end must not be before start")
    if profile is not None and profile not in config.profiles:
        raise ValueError(f"unknown profile {profile!r}; choose from {sorted(config.profiles)}")
    rng = np.random.default_rng(seed)
    days = (end - start).days + 1
    dates = [start + timedelta(days=int(offset)) for offset in rng.integers(0, days, size=count)]
    profiles = np.array([profile or config.profile_for(d) for d in dates])
    simulated = simulate_customers(rng, profiles, config)
    invalid = rng.random(count) < invalid_share

    requests = []
    for i, row in enumerate(simulated.features.loc[:, list(FEATURE_COLUMNS)].to_dict(orient="records")):
        features = {key: _json_value(value) for key, value in row.items()}
        if invalid[i]:
            # A realistic client bug: an impossible value the API must reject with 422.
            features["tenure_months"] = -1
        payload = {"customer_id": f"T{seed}-{i:06d}", "snapshot_date": dates[i].isoformat(), "features": features}
        requests.append(SimulatedRequest(payload, str(profiles[i]), int(simulated.churned[i]), not invalid[i]))
    return requests


def store_hidden_outcomes(ops_url: str, requests: list[SimulatedRequest]) -> None:
    rows = [
        (r.payload["customer_id"], r.payload["snapshot_date"], r.churned, r.profile) for r in requests if r.valid
    ]
    with connect(ops_url) as conn, conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO simulation.customer_outcomes (customer_id, snapshot_date, churned, profile) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (customer_id, snapshot_date) DO NOTHING",
            rows,
        )


def send(inference_url: str, requests: list[SimulatedRequest], concurrency: int = 4) -> TrafficReport:
    report = TrafficReport()

    def call(client: httpx.Client, request: SimulatedRequest):
        start = time.perf_counter()
        try:
            response = client.post("/predict", json=request.payload)
        except httpx.HTTPError:
            return request, None, None
        return request, response, (time.perf_counter() - start) * 1000

    with httpx.Client(base_url=inference_url, timeout=15) as client, ThreadPoolExecutor(max_workers=concurrency) as pool:
        for request, response, latency in pool.map(lambda r: call(client, r), requests):
            report.sent += 1
            report.profiles[request.profile] += 1
            if response is None:
                report.status_counts["connection_error"] += 1
                continue
            report.status_counts[response.status_code] += 1
            report.latencies_ms.append(latency)
            if response.status_code == 200 and response.json()["churn_prediction"]:
                report.predicted_churn += 1
    return report
