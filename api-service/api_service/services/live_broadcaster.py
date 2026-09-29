import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable

from telemetry_shared.models import Telemetry

ALL_FLIGHTS = "*"
"""Subscribing with this flight ID receives every flight."""

MIN_EMIT_INTERVAL_S = 1.0
"""Snapshots go out at most this often per subscriber."""
SETTLE_S = 0.5
"""After a change, wait this long for related updates (e.g. the rest of one ADS-B poll) before sending."""
STALE_AFTER_S = 300.0
"""Flights with no position for this long drop out of snapshots."""


class LiveTelemetryBroadcaster:
    """In-memory live view for GraphQL subscribers.

    Keeps the latest position per flight. Each subscriber receives a snapshot list of the latest positions
    matching its filter (one flight ID, or '*'): immediately on subscribe, then whenever a matching position
    changes, after SETTLE_S (so a burst becomes one snapshot) and at most once per MIN_EMIT_INTERVAL_S.
    Slow subscribers never build a backlog; they get the current snapshot. The cache is per API process
    and starts empty.
    """

    def __init__(
        self,
        min_emit_interval_s: float = MIN_EMIT_INTERVAL_S,
        settle_s: float = SETTLE_S,
        stale_after_s: float = STALE_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_emit_interval_s = min_emit_interval_s
        self._settle_s = settle_s
        self._stale_after_s = stale_after_s
        self._clock = clock
        self._latest: dict[str, tuple[Telemetry, float]] = {}
        self._subscribers: dict[str, set[asyncio.Event]] = defaultdict(set)

    def publish(self, telemetry: Telemetry) -> int:
        """Record the position and wake matching subscribers; returns how many were notified."""
        self._latest[telemetry.flight_id] = (telemetry, self._clock())
        self._prune()
        events = [*self._subscribers.get(telemetry.flight_id, ()), *self._subscribers.get(ALL_FLIGHTS, ())]
        for event in events:
            event.set()
        return len(events)

    def snapshot(self, flight_id: str) -> list[Telemetry]:
        """Latest non-stale positions for a flight ID or '*', ordered by flight ID."""
        self._prune()
        if flight_id == ALL_FLIGHTS:
            entries = list(self._latest.values())
        else:
            entries = [self._latest[flight_id]] if flight_id in self._latest else []
        return sorted((t for t, _ in entries), key=lambda t: t.flight_id)

    async def subscribe(self, flight_id: str) -> AsyncIterator[list[Telemetry]]:
        changed = asyncio.Event()
        self._subscribers[flight_id].add(changed)
        try:
            last_emit = float("-inf")
            if initial := self.snapshot(flight_id):
                yield initial
                last_emit = time.monotonic()
            while True:
                await changed.wait()
                throttle = self._min_emit_interval_s - (time.monotonic() - last_emit)
                await asyncio.sleep(max(self._settle_s, throttle))
                # Cleared after waiting, so positions that arrived meanwhile go out in this snapshot.
                changed.clear()
                if snapshot := self.snapshot(flight_id):
                    yield snapshot
                    last_emit = time.monotonic()
        finally:
            self._subscribers[flight_id].discard(changed)
            if not self._subscribers[flight_id]:
                del self._subscribers[flight_id]

    def _prune(self) -> None:
        cutoff = self._clock() - self._stale_after_s
        for flight_id in [f for f, (_, received_at) in self._latest.items() if received_at < cutoff]:
            del self._latest[flight_id]


broadcaster = LiveTelemetryBroadcaster()
