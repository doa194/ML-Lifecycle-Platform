"""Entry point for `churnctl`.

Administrative lifecycle operations (training, registration, promotion, rollback,
monitoring, retraining) are deliberately local commands instead of HTTP endpoints, so the
public prediction API never exposes them. Each command group lives in its own module and
registers its sub-commands here.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from churn_platform.cli import (
    model,
    monitor,
    pipeline,
    retrain,
    serving,
    simulate,
    stack,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="churnctl", description="Local administration CLI for the churn ML lifecycle platform."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    groups = parser.add_subparsers(dest="group", required=True)
    for module in (stack, pipeline, model, serving, simulate, monitor, retrain):
        module.register(groups)
    return parser


def main(argv: list[str] | None = None) -> int:
    # MLflow prints an agent hint on import; it is noise for an operator CLI.
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    # MLflow and DVC print non-ASCII progress symbols; never crash on a legacy console encoding.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Per-request HTTP logs from client libraries drown out the operator-relevant messages.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return int(args.handler(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
