"""`churnctl serving`: restart the inference service to load a new champion, and show its state.

Promotion and rollback only move the registry alias. The running service keeps the model
it loaded at startup until it is restarted - an explicit, observable step instead of a
silent hot swap.
"""

from __future__ import annotations

import argparse
import time

import httpx

from churn_platform.cli.stack import compose
from churn_platform.settings import load_settings


def register(groups: argparse._SubParsersAction) -> None:
    serving = groups.add_parser("serving", help="inference service operations")
    actions = serving.add_subparsers(dest="action", required=True)
    reload_ = actions.add_parser("reload", help="restart the inference service and wait until it serves the champion")
    reload_.add_argument("--timeout", type=float, default=120)
    reload_.set_defaults(handler=reload_service)
    status = actions.add_parser("status", help="readiness and the served model version")
    status.set_defaults(handler=show_status)


def readiness(base_url: str) -> tuple[int, dict]:
    try:
        response = httpx.get(f"{base_url}/ready", timeout=5)
        return response.status_code, response.json()
    except (httpx.HTTPError, ValueError) as error:
        return 0, {"status": "unreachable", "reason": str(error)}


def reload_service(args: argparse.Namespace) -> int:
    settings = load_settings()
    if compose("restart", "inference") != 0:
        return 1
    deadline = time.monotonic() + args.timeout
    status, body = 0, {}
    while time.monotonic() < deadline:
        status, body = readiness(settings.inference_url)
        if status == 200:
            print(f"inference is ready and serving {body['model_name']} version {body['model_version']}")
            return 0
        time.sleep(2)
    print(f"inference not ready after {args.timeout:.0f}s: {body.get('reason', body)}")
    return 1


def show_status(args: argparse.Namespace) -> int:
    status, body = readiness(load_settings().inference_url)
    print(f"ready: {status == 200}  {body}")
    return 0 if status == 200 else 1
