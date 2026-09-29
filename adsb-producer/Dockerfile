# Build from the repository root so the shared package is in the context:
#   docker build -f adsb-producer/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY shared ./shared
COPY adsb-producer ./adsb-producer
RUN pip install ./shared ./adsb-producer \
    && useradd --create-home appuser

USER appuser
EXPOSE 8001

# PORT is set by hosts such as Render; default to 8001 locally.
CMD ["sh", "-c", "exec uvicorn adsb_producer.main:app --host 0.0.0.0 --port ${PORT:-8001}"]
