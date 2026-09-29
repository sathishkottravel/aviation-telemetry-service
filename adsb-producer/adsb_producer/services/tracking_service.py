import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

from telemetry_shared.adsb.ingestion import AdsbAreaPoller, PollResult
from telemetry_shared.messaging.rabbitmq import RabbitMQ


class AlreadyTrackingError(Exception):
    pass


class NotTrackingError(Exception):
    pass


class IngestUnavailableError(Exception):
    """RabbitMQ is not connected, so polled telemetry would have nowhere to go."""


class TrackingStatus(BaseModel):
    aircraft_id: str
    running: bool
    started_at: datetime | None = None
    last_poll_at: datetime | None = None
    in_area: bool | None = None
    """Whether the last poll found the aircraft inside the configured ADS-B area."""
    last_position_at: datetime | None = None
    published_count: int = 0
    last_error: str | None = None


@dataclass
class _TrackedAircraft:
    aircraft_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_poll_at: datetime | None = None
    in_area: bool | None = None
    last_position_at: datetime | None = None
    published_count: int = 0
    last_error: str | None = None

    def record_result(self, result: PollResult, polled_at: datetime) -> None:
        self.last_poll_at = polled_at
        self.last_error = None
        self.in_area = result.found
        if result.telemetry is not None:
            self.last_position_at = result.telemetry.timestamp
        if result.published:
            self.published_count += 1

    def record_error(self, message: str, polled_at: datetime) -> None:
        self.last_poll_at = polled_at
        self.last_error = message

    def status(self, running: bool) -> TrackingStatus:
        return TrackingStatus(
            aircraft_id=self.aircraft_id,
            running=running,
            started_at=self.started_at,
            last_poll_at=self.last_poll_at,
            in_area=self.in_area,
            last_position_at=self.last_position_at,
            published_count=self.published_count,
            last_error=self.last_error,
        )


class TrackingManager:
    """The set of requested aircraft plus one area poller that serves all of them.

    Start and stop only add or remove IDs; the poller picks up the current set on its next tick, fetches
    the area once, and filters locally. Nothing is persisted; a restart forgets the set.
    """

    def __init__(self, poller: AdsbAreaPoller, rabbitmq: RabbitMQ) -> None:
        self._poller = poller
        self._rabbitmq = rabbitmq
        self._tracked: dict[str, _TrackedAircraft] = {}
        self._task: asyncio.Task | None = None

    def start_polling(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._poller.run(self.aircraft_ids, on_results=self._record_results, on_error=self._record_error),
                name="adsb-area-poller",
            )

    async def stop_polling(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def aircraft_ids(self) -> set[str]:
        return set(self._tracked)

    def start(self, aircraft_id: str) -> TrackingStatus:
        key = _normalize_id(aircraft_id)
        if key in self._tracked:
            raise AlreadyTrackingError(key)
        if not self._rabbitmq.is_connected:
            raise IngestUnavailableError
        tracked = self._tracked[key] = _TrackedAircraft(key)
        return tracked.status(running=True)

    def stop(self, aircraft_id: str) -> TrackingStatus:
        tracked = self._tracked.pop(_normalize_id(aircraft_id), None)
        if tracked is None:
            raise NotTrackingError(aircraft_id)
        return tracked.status(running=False)

    def status(self, aircraft_id: str) -> TrackingStatus:
        key = _normalize_id(aircraft_id)
        tracked = self._tracked.get(key)
        return tracked.status(running=True) if tracked else TrackingStatus(aircraft_id=key, running=False)

    def _record_results(self, results: dict[str, PollResult]) -> None:
        polled_at = datetime.now(UTC)
        for aircraft_id, result in results.items():
            # The aircraft may have been stopped while the poll was in flight.
            if tracked := self._tracked.get(aircraft_id):
                tracked.record_result(result, polled_at)

    def _record_error(self, exc: Exception) -> None:
        polled_at = datetime.now(UTC)
        message = str(exc) or type(exc).__name__
        for tracked in self._tracked.values():
            tracked.record_error(message, polled_at)


def _normalize_id(aircraft_id: str) -> str:
    return aircraft_id.strip().lower()
