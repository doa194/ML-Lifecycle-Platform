"""Commands that create, start, stop and reset the local Docker Compose stack.

`bootstrap` is the one command a new developer runs first: it creates `.env` with random
secrets (so no credential is ever shared or committed), starts the stack, waits until the
health checks pass and wires DVC to the MinIO remote.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

from churn_platform.settings import find_workspace, read_dotenv

PLACEHOLDER = "change-me"

# Services with nothing to do until a champion model exists are still started: the
# inference service reports "not ready" until then instead of failing.
URLS = {
    "MLflow UI": "http://127.0.0.1:5000",
    "MinIO console": "http://127.0.0.1:9001",
    "Inference API": "http://127.0.0.1:8000/docs",
    "Prometheus": "http://127.0.0.1:9090",
    "Grafana": "http://127.0.0.1:3000",
}


def register(groups: argparse._SubParsersAction) -> None:
    boot = groups.add_parser("bootstrap", help="create .env, start the stack, configure DVC")
    boot.set_defaults(handler=bootstrap)

    stack = groups.add_parser("stack", help="manage the Docker Compose stack")
    actions = stack.add_subparsers(dest="action", required=True)
    up = actions.add_parser("up", help="build and start services, wait until healthy")
    up.add_argument("services", nargs="*", help="limit to these services")
    up.set_defaults(handler=lambda a: compose_up(a.services))
    down = actions.add_parser("down", help="stop services (data volumes are kept)")
    down.set_defaults(handler=lambda a: compose("down"))
    status = actions.add_parser("status", help="show service state and health")
    status.set_defaults(handler=lambda a: compose("ps", "--all"))
    restart = actions.add_parser("restart", help="restart services, wait until healthy")
    restart.add_argument("services", nargs="+")
    restart.set_defaults(handler=lambda a: compose("restart", *a.services) or compose_up(a.services))
    reset = actions.add_parser(
        "reset", help="DESTROY all platform state: containers, volumes and local pipeline outputs"
    )
    reset.add_argument("--yes", action="store_true", help="confirm the destructive reset")
    reset.set_defaults(handler=reset_stack)


def compose(*args: str) -> int:
    workspace = find_workspace()
    return subprocess.call(["docker", "compose", *args], cwd=workspace)


def compose_up(services: list[str] | None = None) -> int:
    return compose("up", "--detach", "--build", "--wait", *(services or []))


def ensure_env_file(workspace: Path) -> bool:
    """Create `.env` from `.env.example`, replacing every placeholder with a random secret.

    Returns True when a new file was written. An existing `.env` is never overwritten,
    because the PostgreSQL and MinIO volumes were initialised with its passwords.
    """
    target = workspace / ".env"
    if target.exists():
        return False
    template = (workspace / ".env.example").read_text(encoding="utf-8")
    lines = []
    for line in template.splitlines():
        key, sep, value = line.partition("=")
        if sep and value.strip() == PLACEHOLDER and not key.lstrip().startswith("#"):
            # Hex secrets never start with "-" and need no quoting in URLs, shells or CLIs.
            line = f"{key}={secrets.token_hex(24)}"
        lines.append(line)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def configure_dvc_remote(workspace: Path) -> None:
    """Store the DVC MinIO credentials in `.dvc/config.local` (git-ignored by DVC)."""
    if not (workspace / ".dvc").exists() or shutil.which("dvc") is None:
        print("DVC not initialised here; skipping DVC remote credentials.")
        return
    env = read_dotenv(workspace / ".env")
    for option, value in (
        ("access_key_id", env["DVC_S3_ACCESS_KEY"]),
        ("secret_access_key", env["DVC_S3_SECRET_KEY"]),
    ):
        subprocess.check_call(
            ["dvc", "remote", "modify", "--local", "minio", option, value], cwd=workspace
        )
    print("DVC remote 'minio' credentials written to .dvc/config.local")


def bootstrap(args: argparse.Namespace) -> int:
    workspace = find_workspace()
    if ensure_env_file(workspace):
        print("Created .env with freshly generated secrets.")
    else:
        print("Using existing .env.")
    code = compose_up()
    if code != 0:
        print("docker compose did not reach a healthy state; see `churnctl stack status`.")
        return code
    configure_dvc_remote(workspace)
    print("\nPlatform is up:")
    for name, url in URLS.items():
        print(f"  {name:<14} {url}")
    return 0


def remove_tree(path: Path) -> None:
    """Delete a folder, including files DVC marks read-only (plain rmtree fails on Windows)."""
    if not path.exists():
        return

    def make_writable_and_retry(function, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=make_writable_and_retry)


def reset_stack(args: argparse.Namespace) -> int:
    if not args.yes:
        print("Refusing to reset without --yes: this deletes all models, runs, data and observations.")
        return 2
    workspace = find_workspace()
    code = compose("down", "--volumes", "--remove-orphans")
    for folder in ("data", ".dvc/cache", ".runtime"):
        remove_tree(workspace / folder)
    print("Stack and local pipeline outputs removed. `.env` is kept; run `churnctl bootstrap`.")
    return code
