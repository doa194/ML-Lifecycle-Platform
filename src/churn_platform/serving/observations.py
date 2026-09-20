"""Persists every served prediction as an observation for monitoring and delayed evaluation.

The inference service connects with the `churn_inference` database role, which may only
INSERT into `ops.prediction_observations`; it cannot read or change anything else.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PredictionObservation:
    prediction_id: UUID
    customer_id: str
    snapshot_date: date
    features: dict
    churn_probability: float
    churn_prediction: int
    risk_level: str
    model_name: str
    model_version: str
    model_run_id: str


class ObservationStore(Protocol):
    def open(self) -> None: ...
    def record(self, observation: PredictionObservation) -> None: ...
    def healthy(self) -> bool: ...
    def close(self) -> None: ...


class PostgresObservationStore:
    def __init__(self, url: str):
        # open=False: the pool connects in the background, so a database that is still
        # starting does not block the API from starting (it only affects readiness).
        # check_connection: a connection broken by a database restart is replaced before
        # use instead of failing the next prediction.
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=4,
            open=False,
            timeout=3,
            kwargs={"connect_timeout": 3},
            check=ConnectionPool.check_connection,
        )

    def open(self) -> None:
        self._pool.open(wait=False)

    def record(self, observation: PredictionObservation) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO ops.prediction_observations (prediction_id, customer_id, snapshot_date, features, "
                "churn_probability, churn_prediction, risk_level, model_name, model_version, model_run_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    observation.prediction_id,
                    observation.customer_id,
                    observation.snapshot_date,
                    json.dumps(observation.features),
                    observation.churn_probability,
                    observation.churn_prediction,
                    observation.risk_level,
                    observation.model_name,
                    observation.model_version,
                    observation.model_run_id,
                ),
            )

    def healthy(self) -> bool:
        try:
            with self._pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception as error:  # noqa: BLE001 - any failure means "not healthy"
            log.warning("observation store unavailable: %s", error)
            return False

    def close(self) -> None:
        self._pool.close()
