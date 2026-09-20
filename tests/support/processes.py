"""Real processes for end-to-end tests: the `churnctl` CLI and an inference server.

E2E tests drive the platform exactly like an operator would - through CLI commands and
HTTP - but against a disposable sandbox (own workspace, experiment and model name).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from churn_platform.settings import read_dotenv
from tests.support.data import REPO_ROOT


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sandbox_env(box, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}",
            "CHURN_WORKSPACE": str(box.root),
            "CHURN_MODEL_NAME": box.model_name,
            "CHURN_EXPERIMENT_NAME": box.experiment_name,
            "PYTHONUTF8": "1",
        }
    )
    env.update(extra)
    return env


def churnctl(box, *args: str, check: bool = True, **extra_env: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "churn_platform.cli.main", *args],
        cwd=box.root, env=sandbox_env(box, **extra_env), capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"churnctl {' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result


def json_output(result: subprocess.CompletedProcess[str]) -> dict:
    """The JSON document a command printed (commands print one JSON object to stdout)."""
    text = result.stdout
    if "{" not in text:
        command = " ".join(result.args[3:])
        raise AssertionError(f"`{command}` printed no JSON:\n{text}\n{result.stderr[-2000:]}")
    # Commands may print a human hint after the JSON; decode only the JSON object.
    document, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    return document


class InferenceServer:
    """The real FastAPI service in its own process, serving the sandbox's champion."""

    def __init__(self, box):
        self.box = box
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None

    def start(self, timeout: float = 90) -> InferenceServer:
        """Launch and wait until the service is ready (a verified champion is loaded)."""
        self.launch()
        if not self._wait_for("/ready", timeout):
            raise AssertionError(f"inference server not ready after {timeout}s")
        return self

    def launch(self, timeout: float = 90) -> InferenceServer:
        """Launch and wait until the process answers /health (ready or not).

        The first champion load happens during startup, so once /health answers, /ready
        already reflects whether a verified model could be loaded.
        """
        password = read_dotenv(REPO_ROOT / ".env")["INFERENCE_DB_PASSWORD"]
        env = sandbox_env(
            self.box,
            # Same least-privilege database role as the containerised service.
            OPS_DATABASE_URL=f"postgresql://churn_inference:{password}@127.0.0.1:5432/churn_ops",
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "churn_platform.serving.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port), "--no-access-log"],
            cwd=self.box.root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not self._wait_for("/health", timeout):
            raise AssertionError(f"inference server did not start within {timeout}s")
        return self

    def _wait_for(self, path: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}{path}", timeout=2).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=20)

    def restart(self) -> InferenceServer:
        self.stop()
        return self.start()

    def get(self, path: str) -> httpx.Response:
        return httpx.get(f"{self.url}{path}", timeout=10)

    def post(self, path: str, payload: dict) -> httpx.Response:
        return httpx.post(f"{self.url}{path}", json=payload, timeout=10)
