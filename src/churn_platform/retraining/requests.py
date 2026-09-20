"""Retraining requests in the operations database: create, claim and complete.

The database enforces "at most one open request per model" with a partial unique index, so
two monitoring runs racing each other can never create duplicate retraining work. Claiming
uses `FOR UPDATE SKIP LOCKED`, so two controllers can never process the same request.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime

import psycopg

from churn_platform.storage.db import connect


@dataclass(frozen=True)
class RetrainingRequest:
    request_id: uuid.UUID
    model_name: str
    trigger: str
    reasons: list[str]
    data_as_of: date
    status: str
    attempts: int
    created_at: datetime


_COLUMNS = "request_id, model_name, trigger, reasons, data_as_of, status, attempts, created_at"


def _request(row) -> RetrainingRequest:
    return RetrainingRequest(row[0], row[1], row[2], list(row[3] or []), row[4], row[5], row[6], row[7])


def create_request(
    conn: psycopg.Connection, model_name: str, trigger: str, reasons: list[str], data_as_of: date,
    monitoring_run_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Insert a pending request; returns None when an open request already exists."""
    row = conn.execute(
        "INSERT INTO ops.retraining_requests (request_id, model_name, trigger, reasons, data_as_of, monitoring_run_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING request_id",
        (uuid.uuid4(), model_name, trigger, json.dumps(reasons), data_as_of, monitoring_run_id),
    ).fetchone()
    return row[0] if row else None


def open_request_exists(conn: psycopg.Connection, model_name: str) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM ops.retraining_requests WHERE model_name = %s AND status IN ('pending', 'running'))",
        (model_name,),
    ).fetchone()[0]


def last_request_time(conn: psycopg.Connection, model_name: str) -> datetime | None:
    return conn.execute(
        "SELECT max(created_at) FROM ops.retraining_requests WHERE model_name = %s", (model_name,)
    ).fetchone()[0]


def claim_next(ops_url: str, model_name: str, stale_after_minutes: float, request_id: uuid.UUID | None = None) -> RetrainingRequest | None:
    """Move the oldest pending (or abandoned running, or explicitly retried failed) request to running."""
    with connect(ops_url) as conn:
        row = conn.execute(
            f"""
            UPDATE ops.retraining_requests SET status = 'running', started_at = now(), attempts = attempts + 1, error = NULL
            WHERE request_id = (
                SELECT request_id FROM ops.retraining_requests
                WHERE model_name = %(model)s
                  AND (%(request_id)s::uuid IS NULL OR request_id = %(request_id)s::uuid)
                  AND (status = 'pending'
                       OR (status = 'running' AND started_at < now() - make_interval(secs => %(stale)s * 60))
                       OR (status = 'failed' AND %(request_id)s::uuid IS NOT NULL))
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING {_COLUMNS}
            """,
            {"model": model_name, "request_id": request_id, "stale": float(stale_after_minutes)},
        ).fetchone()
    return _request(row) if row else None


def finish(ops_url: str, request_id: uuid.UUID, status: str, **fields) -> None:
    allowed = {"outcome", "candidate_version", "champion_version_before", "champion_version_after", "error"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown request fields: {unknown}")
    assignments = ", ".join(f"{name} = %({name})s" for name in fields)
    with connect(ops_url) as conn:
        conn.execute(
            f"UPDATE ops.retraining_requests SET status = %(status)s, finished_at = now()"
            f"{', ' + assignments if assignments else ''} WHERE request_id = %(request_id)s",
            {"status": status, "request_id": request_id, **fields},
        )


def list_requests(ops_url: str, model_name: str, limit: int = 20) -> list[dict]:
    with connect(ops_url) as conn:
        rows = conn.execute(
            "SELECT request_id, created_at, trigger, status, attempts, data_as_of, outcome, candidate_version, "
            "champion_version_before, champion_version_after, reasons, error FROM ops.retraining_requests "
            "WHERE model_name = %s ORDER BY created_at DESC LIMIT %s",
            (model_name, limit),
        ).fetchall()
    keys = ("request_id", "created_at", "trigger", "status", "attempts", "data_as_of", "outcome", "candidate_version",
            "champion_before", "champion_after", "reasons", "error")
    return [dict(zip(keys, row, strict=True)) for row in rows]
