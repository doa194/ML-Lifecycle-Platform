"""`churnctl traffic` and `churnctl labels`: simulated production traffic and delayed outcomes.

These commands stand in for the outside world (customers calling the API, the billing
system reporting cancellations). They are demo/test tooling, not platform components.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from churn_platform.config import load_data_config
from churn_platform.settings import load_settings


def register(groups: argparse._SubParsersAction) -> None:
    traffic = groups.add_parser("traffic", help="send simulated customer snapshots to the inference API")
    actions = traffic.add_subparsers(dest="action", required=True)
    send = actions.add_parser("send", help="generate customers dated between --start and --end and request predictions")
    send.add_argument("--start", type=date.fromisoformat, required=True, help="first snapshot date (YYYY-MM-DD)")
    send.add_argument("--end", type=date.fromisoformat, required=True, help="last snapshot date (YYYY-MM-DD)")
    send.add_argument("--count", type=int, default=500)
    send.add_argument("--profile", help="force a distribution profile instead of the configured timeline")
    send.add_argument("--seed", type=int, help="random seed (default: derived from dates and profile)")
    send.add_argument("--invalid-share", type=float, default=0.0, help="share of deliberately invalid requests")
    send.add_argument("--concurrency", type=int, default=4)
    send.set_defaults(handler=send_traffic)

    labels = groups.add_parser("labels", help="delayed ground truth for served predictions")
    label_actions = labels.add_subparsers(dest="action", required=True)
    resolve = label_actions.add_parser("resolve", help="reveal outcomes whose 30-day window closed by --as-of")
    resolve.add_argument("--as-of", type=date.fromisoformat, required=True, help="simulated current date (YYYY-MM-DD)")
    resolve.set_defaults(handler=resolve_labels)


def send_traffic(args: argparse.Namespace) -> int:
    from churn_platform.simulation.traffic import (
        build_requests,
        default_seed,
        send,
        store_hidden_outcomes,
    )

    settings = load_settings()
    config = load_data_config(settings.config_dir)
    seed = args.seed if args.seed is not None else default_seed(args.start, args.end, args.profile)
    requests = build_requests(config, args.start, args.end, args.count, seed, args.profile, args.invalid_share)
    store_hidden_outcomes(settings.ops_database_url, requests)
    report = send(settings.inference_url, requests, args.concurrency)
    print(json.dumps({"seed": seed, **report.summary()}, indent=2))
    return 0 if report.status_counts.get(200) else 1


def resolve_labels(args: argparse.Namespace) -> int:
    from churn_platform.simulation.outcomes import resolve_outcomes

    settings = load_settings()
    config = load_data_config(settings.config_dir)
    summary = resolve_outcomes(settings.ops_database_url, args.as_of, config.generation.label_window_days)
    print(
        f"resolved {summary.resolved} outcomes as of {args.as_of}; "
        f"{summary.still_pending} still inside their outcome window; "
        f"{summary.no_outcome_available} without a known outcome"
    )
    return 0
