"""Append-only audit trail of lifecycle decisions in the operations database.

The MLflow registry holds the current state (which version is champion); this table keeps
the history of how it got there: registrations, gate results, promotions and rollbacks
with the acting user and the reasons. Writing the audit event happens after the registry
change and is best effort: a database outage must not leave a promotion half-done, so a
failed audit write is reported loudly instead of undoing the registry change.
"""

from __future__ import annotations

import getpass
import json
import logging

import psycopg

from churn_platform.storage.db import connect

log = logging.getLogger(__name__)


def current_actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - some containers have no user database entry
        return "unknown"


def record_event(
    ops_url: str, model_name: str, version: str, event_type: str, details: dict, actor: str | None = None
) -> bool:
    try:
        with connect(ops_url) as conn:
            conn.execute(
                "INSERT INTO ops.lifecycle_events (model_name, model_version, event_type, actor, details) "
                "VALUES (%s, %s, %s, %s, %s)",
                (model_name, str(version), event_type, actor or current_actor(), json.dumps(details, default=str)),
            )
        return True
    except psycopg.Error as error:
        log.error("audit event %s for %s v%s NOT recorded: %s", event_type, model_name, version, error)
        return False


def recent_events(ops_url: str, model_name: str, limit: int = 50) -> list[dict]:
    with connect(ops_url) as conn:
        rows = conn.execute(
            "SELECT occurred_at, model_version, event_type, actor, details FROM ops.lifecycle_events "
            "WHERE model_name = %s ORDER BY event_id DESC LIMIT %s",
            (model_name, limit),
        ).fetchall()
    return [
        {"occurred_at": r[0], "version": r[1], "event_type": r[2], "actor": r[3], "details": r[4]} for r in rows
    ]
