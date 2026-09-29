import asyncio
from datetime import UTC, datetime

from api_service.services.live_broadcaster import SUBSCRIBER_BUFFER_SIZE, LiveTelemetryBroadcaster
from telemetry_shared.models import Telemetry


def telemetry(flight_id: str, altitude: float = 0) -> Telemetry:
    return Telemetry(flight_id=flight_id, timestamp=datetime(2026, 9, 29, tzinfo=UTC), latitude=0, longitude=0,
                     altitude=altitude, ground_speed=0, track=0, vertical_rate=0)


async def collect(broadcaster, flight_id, received):
    async for t in broadcaster.subscribe(flight_id):
        received.append(t.flight_id)


async def test_delivers_per_flight_and_to_wildcard_subscribers():
    b = LiveTelemetryBroadcaster()
    one, everything = [], []
    tasks = [asyncio.create_task(collect(b, "4ab563", one)), asyncio.create_task(collect(b, "*", everything))]
    await asyncio.sleep(0)

    assert b.publish(telemetry("4ab563")) == 2
    assert b.publish(telemetry("4ab567")) == 1
    assert b.publish(telemetry("nobody")) == 1  # only the wildcard subscriber
    await asyncio.sleep(0)

    assert one == ["4ab563"]
    assert everything == ["4ab563", "4ab567", "nobody"]
    for task in tasks:
        task.cancel()


async def test_cancelled_subscription_is_removed():
    b = LiveTelemetryBroadcaster()
    task = asyncio.create_task(collect(b, "4ab563", []))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert b.publish(telemetry("4ab563")) == 0
    assert not b._subscribers


async def test_slow_subscriber_keeps_newest_positions():
    b = LiveTelemetryBroadcaster()
    stream = b.subscribe("4ab563")
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)  # subscribed, but the consumer hasn't taken anything yet
    for altitude in range(SUBSCRIBER_BUFFER_SIZE + 5):
        b.publish(telemetry("4ab563", altitude))
    # 105 positions into a 100-slot buffer: the 5 oldest were dropped, the newest 100 are kept in order.
    assert (await first).altitude == 5
    assert (await anext(stream)).altitude == 6
    await stream.aclose()
