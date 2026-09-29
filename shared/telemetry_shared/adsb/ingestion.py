"""Turn ADSB.lol aircraft records into Telemetry and publish them into the RabbitMQ ingest pipeline."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from telemetry_shared.adsb.client import AdsbLolClient, AdsbRateLimitedError, AdsbSnapshot
from telemetry_shared.config import Settings
from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_shared.models import AreaAircraft, Telemetry

logger = logging.getLogger(__name__)


def _callsign(raw: dict[str, Any]) -> str | None:
    return (raw.get("flight") or "").strip() or None


def _first(raw: dict[str, Any], *keys: str) -> Any:
    return next((raw[k] for k in keys if raw.get(k) is not None), None)


def index_by_id(aircraft: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Look up records by lowercase ICAO hex or callsign. A hex match wins over a callsign match."""
    index: dict[str, dict[str, Any]] = {}
    for raw in aircraft:
        if callsign := _callsign(raw):
            index.setdefault(callsign.lower(), raw)
    for raw in aircraft:
        if hex_id := (raw.get("hex") or "").lower():
            index[hex_id] = raw
    return index


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


def list_area_aircraft(snapshot: AdsbSnapshot) -> list[AreaAircraft]:
    """Aircraft in the snapshot that tracking could publish (same rules as normalize()), nearest first."""
    aircraft = []
    for raw in snapshot.aircraft:
        telemetry = normalize(raw, snapshot.now)
        if telemetry is None or not raw.get("hex"):
            continue
        aircraft.append(
            AreaAircraft(
                icao_hex=telemetry.flight_id,
                callsign=telemetry.callsign,
                latitude=telemetry.latitude,
                longitude=telemetry.longitude,
                altitude=telemetry.altitude,
                ground_speed=telemetry.ground_speed,
                track=telemetry.track,
                distance_nm=raw.get("dst"),
            )
        )
    return sorted(aircraft, key=lambda a: (a.distance_nm is None, a.distance_nm))


@dataclass
class PollResult:
    found: bool
    """The aircraft was in the ADSB.lol snapshot with a usable position."""
    published: bool
    """A new position was published (False when the position had not changed since the last poll)."""
    telemetry: Telemetry | None = None


class AdsbAreaPoller:
    """A single loop that fetches the configured area once per interval and publishes the requested aircraft.

    The set of requested aircraft is read on every tick, so adding or removing an ID takes effect on the
    next poll. No request is made while the set is empty. The last snapshot is kept so the area can be
    listed without extra upstream requests.
    """

    def __init__(self, client: AdsbLolClient, rabbitmq: RabbitMQ, settings: Settings) -> None:
        self._client = client
        self._rabbitmq = rabbitmq
        self._settings = settings
        self._last_published: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._snapshot: AdsbSnapshot | None = None
        self._fetched_at = 0.0

    async def _fetch(self) -> AdsbSnapshot:
        async with self._lock:
            snapshot = await self._client.fetch_area(
                self._settings.adsb_latitude, self._settings.adsb_longitude, self._settings.adsb_radius_nm
            )
            self._snapshot, self._fetched_at = snapshot, time.monotonic()
            return snapshot

    async def latest_snapshot(self) -> AdsbSnapshot:
        """The latest area snapshot, fetching only if the poller hasn't refreshed it recently.

        If a fetch fails (rate limited, upstream down) an older snapshot is returned when there is one.
        """
        # 1.5x: a poll cycle takes the interval plus request time, so a running poller always counts as fresh.
        max_age_s = self._settings.adsb_poll_interval * 1.5
        if self._snapshot is not None and time.monotonic() - self._fetched_at < max_age_s:
            return self._snapshot
        try:
            return await self._fetch()
        except Exception:
            if self._snapshot is None:
                raise
            return self._snapshot

    async def poll_once(self, aircraft_ids: set[str]) -> dict[str, PollResult]:
        snapshot = await self._fetch()
        by_id = index_by_id(snapshot.aircraft)
        # Forget de-duplication state for aircraft that are no longer requested.
        self._last_published = {k: v for k, v in self._last_published.items() if k in aircraft_ids}

        results: dict[str, PollResult] = {}
        for aircraft_id in aircraft_ids:
            raw = by_id.get(aircraft_id)
            telemetry = normalize(raw, snapshot.now) if raw else None
            if telemetry is None:
                results[aircraft_id] = PollResult(found=False, published=False)
                continue

            # ADSB.lol repeats the last known position until a new one arrives; only publish real updates.
            last = self._last_published.get(aircraft_id)
            if last is not None and telemetry.timestamp <= last:
                results[aircraft_id] = PollResult(found=True, published=False, telemetry=telemetry)
                continue

            await self._rabbitmq.publish_telemetry(telemetry)
            self._last_published[aircraft_id] = telemetry.timestamp
            results[aircraft_id] = PollResult(found=True, published=True, telemetry=telemetry)
        return results

    async def run(
        self,
        get_aircraft_ids: Callable[[], set[str]],
        on_results: Callable[[dict[str, PollResult]], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Poll until cancelled. A failed poll is reported and retried on the next interval."""
        while True:
            aircraft_ids = get_aircraft_ids()
            if aircraft_ids:
                try:
                    results = await self.poll_once(aircraft_ids)
                    if on_results:
                        on_results(results)
                except AdsbRateLimitedError as exc:
                    # The client already logged the 429 when it started the pause.
                    logger.debug("ADS-B poll skipped: %s", exc)
                    if on_error:
                        on_error(exc)
                except Exception as exc:
                    logger.warning("ADS-B poll failed: %s", exc)
                    if on_error:
                        on_error(exc)
            await asyncio.sleep(self._settings.adsb_poll_interval)
