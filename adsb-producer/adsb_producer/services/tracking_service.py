import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

from telemetry_shared.adsb.ingestion import AdsbIngestor, PollResult
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
    task: asyncio.Task | None = None
    last_poll_at: datetime | None = None
    in_area: bool | None = None
    last_position_at: datetime | None = None
    published_count: int = 0
    last_error: str | None = None

    def record_result(self, result: PollResult) -> None:
        self.last_poll_at = datetime.now(UTC)
        self.last_error = None
        self.in_area = result.found
        if result.telemetry is not None:
            self.last_position_at = result.telemetry.timestamp
        if result.published:
            self.published_count += 1

    def record_error(self, exc: Exception) -> None:
        self.last_poll_at = datetime.now(UTC)
        self.last_error = str(exc) or type(exc).__name__

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
    """One in-memory polling task per tracked aircraft. Nothing is persisted; a restart forgets them."""

    def __init__(self, ingestor: AdsbIngestor, rabbitmq: RabbitMQ) -> None:
        self._ingestor = ingestor
        self._rabbitmq = rabbitmq
        self._tracked: dict[str, _TrackedAircraft] = {}

    def start(self, aircraft_id: str) -> TrackingStatus:
        key = _normalize_id(aircraft_id)
        if key in self._tracked:
            raise AlreadyTrackingError(key)
        if not self._rabbitmq.is_connected:
            raise IngestUnavailableError

        tracked = _TrackedAircraft(key)
        tracked.task = asyncio.create_task(
            self._ingestor.run(key, on_result=tracked.record_result, on_error=tracked.record_error),
            name=f"adsb-track:{key}",
        )
        self._tracked[key] = tracked
        return tracked.status(running=True)

    async def stop(self, aircraft_id: str) -> TrackingStatus:
        tracked = self._tracked.pop(_normalize_id(aircraft_id), None)
        if tracked is None:
            raise NotTrackingError(aircraft_id)
        await _cancel(tracked.task)
        return tracked.status(running=False)

    def status(self, aircraft_id: str) -> TrackingStatus:
        key = _normalize_id(aircraft_id)
        tracked = self._tracked.get(key)
        return tracked.status(running=True) if tracked else TrackingStatus(aircraft_id=key, running=False)

    async def stop_all(self) -> None:
        tracked, self._tracked = list(self._tracked.values()), {}
        for t in tracked:
            await _cancel(t.task)


def _normalize_id(aircraft_id: str) -> str:
    return aircraft_id.strip().lower()


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
