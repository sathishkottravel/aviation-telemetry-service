"""Turn ADSB.lol aircraft records into Telemetry and publish them into the RabbitMQ ingest pipeline."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from telemetry_shared.adsb.client import AdsbLolClient, AdsbRateLimitedError
from telemetry_shared.config import Settings
from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_shared.models import Telemetry

logger = logging.getLogger(__name__)


def _callsign(raw: dict[str, Any]) -> str | None:
    return (raw.get("flight") or "").strip() or None


def _first(raw: dict[str, Any], *keys: str) -> Any:
    return next((raw[k] for k in keys if raw.get(k) is not None), None)


def matches(raw: dict[str, Any], aircraft_id: str) -> bool:
    """True if aircraft_id is this record's ICAO hex or callsign (case-insensitive)."""
    wanted = aircraft_id.strip().lower()
    callsign = _callsign(raw)
    return (raw.get("hex") or "").lower() == wanted or (callsign is not None and callsign.lower() == wanted)


def normalize(raw: dict[str, Any], now: datetime) -> Telemetry | None:
    """Map one ADSB.lol aircraft record to Telemetry, or None if it has no usable position or altitude."""
    if raw.get("lat") is None or raw.get("lon") is None:
        return None

    callsign = _callsign(raw)
    flight_id = (raw.get("hex") or "").lower() or callsign
    if not flight_id:
        return None

    altitude = _first(raw, "alt_baro", "alt_geom")
    if altitude == "ground":
        altitude = 0
    if altitude is None:
        return None

    age_s = _first(raw, "seen_pos", "seen") or 0
    return Telemetry(
        flight_id=flight_id,
        callsign=callsign,
        timestamp=now - timedelta(seconds=age_s),
        latitude=raw["lat"],
        longitude=raw["lon"],
        altitude=altitude,
        ground_speed=_first(raw, "gs") or 0,
        track=(_first(raw, "track", "true_heading") or 0) % 360,
        vertical_rate=_first(raw, "baro_rate", "geom_rate") or 0,
    )


@dataclass
class PollResult:
    found: bool
    """The aircraft was in the ADSB.lol snapshot with a usable position."""
    published: bool
    """A new position was published (False when the position had not changed since the last poll)."""
    telemetry: Telemetry | None = None


class AdsbIngestor:
    """Polls the configured area for one aircraft at a time and publishes new positions."""

    def __init__(self, client: AdsbLolClient, rabbitmq: RabbitMQ, settings: Settings) -> None:
        self._client = client
        self._rabbitmq = rabbitmq
        self._settings = settings
        self._last_published: dict[str, datetime] = {}

    async def poll_once(self, aircraft_id: str) -> PollResult:
        snapshot = await self._client.fetch_area(
            self._settings.adsb_latitude, self._settings.adsb_longitude, self._settings.adsb_radius_nm
        )
        telemetry = next(
            (t for raw in snapshot.aircraft if matches(raw, aircraft_id) and (t := normalize(raw, snapshot.now))),
            None,
        )
        if telemetry is None:
            return PollResult(found=False, published=False)

        # ADSB.lol repeats the last known position until a new one arrives; only publish real updates.
        last = self._last_published.get(aircraft_id)
        if last is not None and telemetry.timestamp <= last:
            return PollResult(found=True, published=False, telemetry=telemetry)

        await self._rabbitmq.publish_telemetry(telemetry)
        self._last_published[aircraft_id] = telemetry.timestamp
        return PollResult(found=True, published=True, telemetry=telemetry)

    async def run(
        self,
        aircraft_id: str,
        on_result: Callable[[PollResult], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Poll until cancelled. A failed poll is reported and retried on the next interval."""
        try:
            while True:
                try:
                    result = await self.poll_once(aircraft_id)
                    if on_result:
                        on_result(result)
                except AdsbRateLimitedError as exc:
                    # The client already logged the 429 once; don't repeat it for every tracker.
                    logger.debug("ADS-B poll for %s skipped: %s", aircraft_id, exc)
                    if on_error:
                        on_error(exc)
                except Exception as exc:
                    logger.warning("ADS-B poll for %s failed: %s", aircraft_id, exc)
                    if on_error:
                        on_error(exc)
                await asyncio.sleep(self._settings.adsb_poll_interval)
        finally:
            self._last_published.pop(aircraft_id, None)
