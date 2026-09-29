import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator

from telemetry_shared.models import Telemetry

ALL_FLIGHTS = "*"
"""Subscribing with this flight ID receives telemetry for every flight."""

SUBSCRIBER_BUFFER_SIZE = 100


class LiveTelemetryBroadcaster:
    """In-memory fan-out of live telemetry to GraphQL subscribers, keyed by flight ID."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[Telemetry]]] = defaultdict(set)

    async def subscribe(self, flight_id: str) -> AsyncIterator[Telemetry]:
        queue: asyncio.Queue[Telemetry] = asyncio.Queue(maxsize=SUBSCRIBER_BUFFER_SIZE)
        self._subscribers[flight_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[flight_id].discard(queue)
            if not self._subscribers[flight_id]:
                del self._subscribers[flight_id]

    def publish(self, telemetry: Telemetry) -> int:
        """Deliver to this flight's subscribers and '*' subscribers; returns how many received it."""
        queues = [*self._subscribers.get(telemetry.flight_id, ()), *self._subscribers.get(ALL_FLIGHTS, ())]
        for queue in queues:
            if queue.full():
                # A slow client should see the newest position, not a backlog.
                queue.get_nowait()
            queue.put_nowait(telemetry)
        return len(queues)


broadcaster = LiveTelemetryBroadcaster()
