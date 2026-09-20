# syntax=docker/dockerfile:1.7
# Platform image with two runtime targets built from the same locked dependencies:
#   inference - the FastAPI prediction service        (build arg EXTRA=serving)
#   worker    - monitoring worker and one-shot jobs    (build arg EXTRA=monitoring)
# Each target installs only its own optional dependencies, so the inference image does
# not carry Evidently and the worker does not carry the web server.
ARG PYTHON_IMAGE=python:3.12-slim-trixie

FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
ARG EXTRA
WORKDIR /build
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra "${EXTRA}"
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra "${EXTRA}"

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    CHURN_WORKSPACE=/app \
    CHURN_CONFIG_DIR=/app/config \
    MLFLOW_DISABLE_AGENT_HINT=1 \
    HOME=/tmp
# Unprivileged user; the containers also run with a read-only root filesystem.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin churn
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY config ./config
USER 10001

FROM runtime AS inference
EXPOSE 8000
# Liveness only: a missing champion makes /ready fail, not the container health check.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "churn_platform.serving.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS worker
# Evidently sends anonymous usage telemetry unless told not to.
ENV DO_NOT_TRACK=1 \
    EVIDENTLY_DISABLE_TELEMETRY=1
CMD ["churnctl", "monitor", "run", "--loop"]
