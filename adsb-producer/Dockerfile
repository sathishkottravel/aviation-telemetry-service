# Build from the repository root so the workspace and shared package are in the context:
#   docker build -f adsb-producer/Dockerfile .

# Stage 1: resolve the exact dependency versions (with hashes) from uv.lock.
FROM ghcr.io/astral-sh/uv:0.6.16 AS uv
FROM python:3.12-slim AS requirements
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY shared/pyproject.toml shared/
COPY api-service/pyproject.toml api-service/
COPY telemetry-worker/pyproject.toml telemetry-worker/
COPY adsb-producer/pyproject.toml adsb-producer/
RUN uv export --frozen --package adsb-producer --no-dev --no-emit-workspace -o requirements.txt

# Stage 2: runtime image with the locked dependencies, then the workspace packages without resolving again.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY --from=requirements /src/requirements.txt ./
RUN pip install --require-hashes -r requirements.txt

COPY shared ./shared
COPY adsb-producer ./adsb-producer
RUN pip install --no-deps ./shared ./adsb-producer \
    && useradd --create-home appuser

USER appuser
EXPOSE 8001

# PORT is set by hosts such as Render; default to 8001 locally.
CMD ["sh", "-c", "exec uvicorn adsb_producer.main:app --host 0.0.0.0 --port ${PORT:-8001}"]
