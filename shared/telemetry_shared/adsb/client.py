import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MIN_BACKOFF_S = 10.0
MAX_BACKOFF_S = 120.0


class AdsbRateLimitedError(Exception):
    """ADSB.lol answered 429; requests are paused until the back-off expires."""


@dataclass(frozen=True)
class AdsbSnapshot:
    now: datetime
    """Server time of the snapshot; each aircraft's position age (seen_pos) is relative to it."""
    aircraft: list[dict[str, Any]]


class AdsbLolClient:
    """Area queries against the ADSB.lol v2 API. A 429 pauses requests with a growing back-off."""

    def __init__(self, base_url: str, timeout_s: float, user_agent: str) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, headers={"User-Agent": user_agent})
        self._backoff_s = 0.0
        self._paused_until = 0.0

    async def fetch_area(self, lat: float, lon: float, radius_nm: float) -> AdsbSnapshot:
        now = time.monotonic()
        if now < self._paused_until:
            raise AdsbRateLimitedError(f"ADSB.lol rate limited; resuming in {self._paused_until - now:.0f}s")

        response = await self._http.get(f"/v2/point/{lat}/{lon}/{radius_nm:g}")
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            self._backoff_s = min(max(self._backoff_s * 2, MIN_BACKOFF_S), MAX_BACKOFF_S)
            pause = _retry_after_s(response) or self._backoff_s
            self._paused_until = time.monotonic() + pause
            logger.warning("ADSB.lol rate limited (429); pausing requests for %.0fs", pause)
            raise AdsbRateLimitedError(f"ADSB.lol rate limited; resuming in {pause:.0f}s")
        response.raise_for_status()
        self._backoff_s = 0.0

        body = response.json()
        snapshot = AdsbSnapshot(
            now=datetime.fromtimestamp(body["now"] / 1000, tz=UTC),
            aircraft=body.get("ac") or [],
        )
        logger.debug("Fetched %d aircraft from ADSB.lol", len(snapshot.aircraft))
        return snapshot

    async def close(self) -> None:
        await self._http.aclose()


def _retry_after_s(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None
