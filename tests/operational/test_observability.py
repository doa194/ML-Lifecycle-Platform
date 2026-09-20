"""Observability checks on the live stack: Prometheus scrapes the service, traffic moves the
metrics the dashboards use, and Grafana's data sources and dashboards are provisioned.
"""

from __future__ import annotations

import time
from datetime import date

import httpx
import pytest

from churn_platform.config import load_data_config
from churn_platform.settings import read_dotenv
from churn_platform.simulation.traffic import (
    build_requests,
    send,
    store_hidden_outcomes,
)
from tests.support.data import REPO_ROOT

pytestmark = pytest.mark.operational

PROMETHEUS = "http://127.0.0.1:9090"
GRAFANA = "http://127.0.0.1:3000"


def prometheus_value(query: str) -> float:
    result = httpx.get(f"{PROMETHEUS}/api/v1/query", params={"query": query}, timeout=10).json()["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0


def test_prometheus_scrapes_the_inference_service(live_inference):
    targets = httpx.get(f"{PROMETHEUS}/api/v1/targets", timeout=10).json()["data"]["activeTargets"]
    inference = [t for t in targets if t["labels"]["job"] == "inference"]

    assert inference and inference[0]["health"] == "up"
    assert prometheus_value("max(churn_model_loaded)") == 1.0
    rules = httpx.get(f"{PROMETHEUS}/api/v1/rules", timeout=10).json()["data"]["groups"]
    assert {r["name"] for g in rules for r in g["rules"]} >= {"InferenceDown", "ChampionNotLoaded", "HighPredictionErrorRate"}


def test_generated_traffic_moves_the_dashboard_metrics(stack, live_inference):
    served_before = prometheus_value('sum(churn_http_requests_total{route="/predict",status="200"})')
    rejected_before = prometheus_value('sum(churn_http_requests_total{route="/predict",status="422"})')
    requests = build_requests(load_data_config(stack.config_dir), date(2025, 12, 1), date(2025, 12, 31), 60, seed=990001,
                              invalid_share=0.2)
    store_hidden_outcomes(stack.ops_database_url, requests)

    report = send(live_inference, requests, concurrency=4)
    time.sleep(12)  # at least two 5-second scrape intervals

    served = prometheus_value('sum(churn_http_requests_total{route="/predict",status="200"})') - served_before
    rejected = prometheus_value('sum(churn_http_requests_total{route="/predict",status="422"})') - rejected_before
    assert served == report.status_counts[200]
    assert rejected == report.status_counts[422] > 0


def test_grafana_serves_provisioned_datasources_and_dashboards(stack):
    auth = ("admin", read_dotenv(REPO_ROOT / ".env")["GRAFANA_ADMIN_PASSWORD"])

    health = {uid: httpx.get(f"{GRAFANA}/api/datasources/uid/{uid}/health", auth=auth, timeout=10).json()["status"]
              for uid in ("prometheus", "churn-ops")}
    dashboards = {d["uid"] for d in httpx.get(f"{GRAFANA}/api/search", params={"type": "dash-db"}, auth=auth, timeout=10).json()}

    assert health == {"prometheus": "OK", "churn-ops": "OK"}
    assert {"churn-inference-ops", "churn-model-monitoring"} <= dashboards
