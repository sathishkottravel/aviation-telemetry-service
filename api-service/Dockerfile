# Build from the repository root so the shared package is in the context:
#   docker build -f api-service/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY shared ./shared
COPY api-service ./api-service
RUN pip install ./shared ./api-service \
    && useradd --create-home appuser

USER appuser
EXPOSE 8000

# PORT is set by hosts such as Render; default to 8000 locally.
CMD ["sh", "-c", "exec uvicorn api_service.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
