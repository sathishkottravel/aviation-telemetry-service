"""Keeps sleeping hosts awake while the API is in use (Render free tier: services sleep after 15 min idle).

Pings every WAKE_URLS entry on startup, so waking the API also wakes the producer and worker, then every
10 minutes. Once nobody uses the API it sleeps too, and the pings stop.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 600
# Long enough to cover a Render cold start.
TIMEOUT_SECONDS = 90


async def ping_all(client: httpx.AsyncClient, urls: list[str]) -> None:
    async def ping(url: str) -> None:
        try:
            response = await client.get(url)
            if not response.is_success:
                logger.warning("Keep-warm ping %s returned HTTP %s", url, response.status_code)
        except httpx.HTTPError as exc:
            logger.warning("Keep-warm ping %s failed: %s", url, exc)

    await asyncio.gather(*(ping(url) for url in urls))


async def run(urls: list[str], interval: float = INTERVAL_SECONDS) -> None:
    """Ping until cancelled."""
    logger.info("Keep-warm pinging %d URL(s) every %d s", len(urls), interval)
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        while True:
            await ping_all(client, urls)
            await asyncio.sleep(interval)
