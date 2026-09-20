"""Helpers for tests that need the running Docker Compose stack.

Integration, E2E and operational tests share these so that "is the stack up?" checks,
container restarts and health waits behave the same everywhere.
"""

from __future__ import annotations

import json
import subprocess
import time

import httpx
import psycopg

from churn_platform.settings import Settings, find_workspace


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=find_workspace(),
        capture_output=True,
        text=True,
        check=check,
    )


def service_states() -> dict[str, dict]:
    """Map service name -> `docker compose ps` entry (includes State and Health)."""
    output = compose("ps", "--all", "--format", "json").stdout
    entries = [json.loads(line) for line in output.splitlines() if line.strip()]
    return {entry["Service"]: entry for entry in entries}


def wait_until_healthy(service: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service_states().get(service, {})
        if state.get("Health") == "healthy" or (
            state.get("State") == "running" and not state.get("Health")
        ):
            return
        time.sleep(2)
    raise AssertionError(f"service {service} not healthy after {timeout}s: {state}")


def restart_and_wait(*services: str) -> None:
    compose("restart", *services)
    for service in services:
        wait_until_healthy(service)


def mlflow_is_up(settings: Settings) -> bool:
    try:
        return httpx.get(f"{settings.mlflow_tracking_uri}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def postgres_is_up(settings: Settings) -> bool:
    try:
        with psycopg.connect(settings.ops_database_url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False
