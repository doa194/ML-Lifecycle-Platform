"""Fixtures for operational tests against the live Docker Compose stack.

Several checks need the containerised inference service to be serving a champion (the
state after the demo walkthrough). They fail with a clear instruction instead of guessing.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(scope="session")
def live_inference(stack):
    try:
        response = httpx.get(f"{stack.inference_url}/ready", timeout=5)
    except httpx.HTTPError as error:
        pytest.fail(f"inference service unreachable ({error}); run `churnctl bootstrap`")
    if response.status_code != 200:
        pytest.fail(
            f"inference is not ready ({response.json().get('reason')}); promote a champion first "
            "(see docs/demo-walkthrough.md) and run `churnctl serving reload`"
        )
    return stack.inference_url
