"""PostgreSQL access for the operations database (`churn_ops`) and its schema migrations.

Migrations are plain SQL files shipped inside the package, applied in file-name order and
recorded in `ops.schema_migrations`, so `churnctl db migrate` (and the `db-migrate` setup
container) can run any number of times safely.
"""

from __future__ import annotations

import logging
from importlib import resources

import psycopg

log = logging.getLogger(__name__)


def connect(url: str, **kwargs) -> psycopg.Connection:
    return psycopg.connect(url, connect_timeout=5, **kwargs)


def _migration_files() -> list[tuple[str, str]]:
    folder = resources.files("churn_platform.storage") / "migrations"
    files = sorted((entry.name, entry.read_text(encoding="utf-8")) for entry in folder.iterdir() if entry.name.endswith(".sql"))
    return files


def migrate(url: str) -> list[str]:
    """Apply pending migrations; returns the names of the files that were applied."""
    applied = []
    with connect(url) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS ops")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ops.schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT version FROM ops.schema_migrations")}
        for name, sql in _migration_files():
            if name in done:
                continue
            # Each migration commits atomically together with its bookkeeping row.
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO ops.schema_migrations (version) VALUES (%s)", (name,))
            applied.append(name)
            log.info("applied migration %s", name)
    return applied
