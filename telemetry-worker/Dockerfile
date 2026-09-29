# Build from the repository root so the shared package is in the context:
#   docker build -f telemetry-worker/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY shared ./shared
COPY telemetry-worker ./telemetry-worker
RUN pip install ./shared ./telemetry-worker \
    && useradd --create-home appuser

USER appuser

CMD ["telemetry-worker"]
