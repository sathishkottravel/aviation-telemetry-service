"""One health call for the whole backend (GET /health/all): the api, plus the producer and worker over HTTP.

Only status values go into the response, never URLs or error details. Checks run concurrently, each with a short
timeout, so a sleeping (Render free tier) or missing service shows as "unreachable" instead of blocking.
"""

import asyncio
import logging
from typing import Any

import httpx

from api_service.services import adsb_service
from telemetry_shared.config import get_settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5


async def check_all(api_rabbitmq_connected: bool) -> dict[str, Any]:
    producer, worker = await asyncio.gather(_producer(), _worker())
    services = {"api": {"status": "ok", "rabbitmq": api_rabbitmq_connected}, "producer": producer, "worker": worker}
    healthy = all(s["status"] in ("ok", "not_configured") for s in services.values())
    return {"status": "ok" if healthy else "degraded", "services": services}


async def _producer() -> dict[str, Any]:
    try:
        return _summary(await asyncio.wait_for(adsb_service.health(), TIMEOUT_SECONDS))
    except Exception as exc:  # any failure (timeout, refused, bad body) means unreachable
        logger.debug("Producer health check failed: %s", exc)
        return {"status": "unreachable"}


async def _worker() -> dict[str, Any]:
    url = get_settings().worker_url
    if not url:
        return {"status": "not_configured"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(url.rstrip("/") + "/health")
            response.raise_for_status()
            return _summary(response.json())
    except Exception as exc:
        logger.debug("Worker health check failed: %s", exc)
        return {"status": "unreachable"}


def _summary(body: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok" if body.get("status") == "ok" else "unreachable", "rabbitmq": bool(body.get("rabbitmq"))}
