import asyncio
from datetime import UTC, datetime

import pytest

from api_service.services.live_broadcaster import LiveTelemetryBroadcaster
from telemetry_shared.models import Telemetry


def telemetry(flight_id: str, altitude: float = 0) -> Telemetry:
    return Telemetry(flight_id=flight_id, timestamp=datetime(2026, 9, 29, tzinfo=UTC), latitude=0, longitude=0,
                     altitude=altitude, ground_speed=0, track=0, vertical_rate=0)


def summary(snapshot: list[Telemetry]) -> list[tuple[str, float]]:
    return [(t.flight_id, t.altitude) for t in snapshot]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def broadcaster():
    return LiveTelemetryBroadcaster(min_emit_interval_s=0.05, settle_s=0.05)


async def next_snapshot(stream, timeout=1.0):
    return await asyncio.wait_for(anext(stream), timeout)


async def test_sends_current_snapshot_on_subscribe(broadcaster):
    broadcaster.publish(telemetry("b", 2))
    broadcaster.publish(telemetry("a", 1))
    stream = broadcaster.subscribe("*")
    assert summary(await next_snapshot(stream)) == [("a", 1), ("b", 2)]  # sorted by flight ID
    await stream.aclose()


async def test_nothing_is_sent_while_there_is_no_data(broadcaster):
    stream = broadcaster.subscribe("*")
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.1)
    assert not pending.done()
    broadcaster.publish(telemetry("a"))
    assert summary(await asyncio.wait_for(pending, 1)) == [("a", 0)]
    await stream.aclose()


async def test_single_flight_filter_gets_a_one_element_list(broadcaster):
    broadcaster.publish(telemetry("other"))
    stream = broadcaster.subscribe("a")
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    assert broadcaster.publish(telemetry("other", 5)) == 0  # not a matching subscriber
    assert broadcaster.publish(telemetry("a", 7)) == 1
    assert summary(await asyncio.wait_for(pending, 1)) == [("a", 7)]
    await stream.aclose()


async def test_burst_collapses_into_one_snapshot_with_latest_positions(broadcaster):
    broadcaster.publish(telemetry("a", 1))
    stream = broadcaster.subscribe("*")
    assert summary(await next_snapshot(stream)) == [("a", 1)]

    # Within the throttle window: one ADS-B poll's worth of updates, including a newer position for "a".
    for flight_id, altitude in [("b", 2), ("c", 3), ("a", 10)]:
        broadcaster.publish(telemetry(flight_id, altitude))
    assert summary(await next_snapshot(stream)) == [("a", 10), ("b", 2), ("c", 3)]

    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.15)
    assert not pending.done()  # no extra snapshot left over from the burst
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await stream.aclose()


async def test_updates_spread_over_the_settle_window_arrive_as_one_snapshot():
    broadcaster = LiveTelemetryBroadcaster(min_emit_interval_s=0, settle_s=0.2)
    stream = broadcaster.subscribe("*")
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    for flight_id in ("a", "b", "c"):  # like a worker processing one ADS-B poll message by message
        broadcaster.publish(telemetry(flight_id))
        await asyncio.sleep(0.03)
    assert summary(await asyncio.wait_for(pending, 1)) == [("a", 0), ("b", 0), ("c", 0)]
    await stream.aclose()


async def test_stale_flights_drop_out():
    clock = FakeClock()
    broadcaster = LiveTelemetryBroadcaster(min_emit_interval_s=0, settle_s=0, stale_after_s=300, clock=clock)
    broadcaster.publish(telemetry("old"))
    clock.now += 301
    broadcaster.publish(telemetry("new"))
    assert summary(broadcaster.snapshot("*")) == [("new", 0)]
    assert broadcaster.snapshot("old") == []


async def test_cancelled_subscription_is_removed(broadcaster):
    stream = broadcaster.subscribe("a")
    task = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    assert broadcaster.publish(telemetry("x")) == 0
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await stream.aclose()
    assert broadcaster.publish(telemetry("a")) == 0
    assert not broadcaster._subscribers
