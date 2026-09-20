"""Command-line dispatcher for the DVC stages: `python -m churn_platform.pipeline <stage>`."""

from __future__ import annotations

import logging
import os
import sys

from churn_platform.pipeline import stages


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in stages.STAGES:
        print(f"usage: python -m churn_platform.pipeline {{{','.join(stages.STAGES)}}}", file=sys.stderr)
        return 2
    return stages.STAGES[args[0]]()


if __name__ == "__main__":
    sys.exit(main())
