# MLflow tracking + model registry server.
# Metadata goes to PostgreSQL; artifacts go to MinIO through MLflow's artifact proxy
# (`--serve-artifacts`), so only this container holds the MinIO credentials for models.
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin mlflow

USER 10001
WORKDIR /home/mlflow
EXPOSE 5000

HEALTHCHECK --interval=10s --timeout=5s --start-period=40s --retries=6 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["mlflow", "server"]
