"""RabbitMQ publish/consume and the worker pipeline against real RabbitMQ and MongoDB."""

from datetime import UTC, datetime
from functools import partial

import pytest
from opentelemetry.sdk.trace import TracerProvider

from telemetry_shared.database import mongo
from telemetry_shared.messaging.rabbitmq import SOURCE_HEADER, RabbitMQ
from telemetry_shared.models import Telemetry
from telemetry_worker.workers.telemetry_consumer import handle_message

from .conftest import wait_for

pytestmark = pytest.mark.integration


def collect_into(sink: list):
    """An aio-pika consumer callback that stores each message's Telemetry in sink."""

    async def handler(message):
        sink.append(Telemetry.model_validate_json(message.body))

    return handler


TELEMETRY = Telemetry(flight_id="ITEST1", callsign="ITEST", timestamp=datetime(2026, 9, 29, 12, tzinfo=UTC),
                      latitude=59.3, longitude=18.0, altitude=3000, ground_speed=200, track=90, vertical_rate=0)


async def test_ingest_message_carries_source_and_trace_context(rabbitmq, rabbit_settings):
    received = []

    async def capture(message):
        async with message.process():
            received.append(message)

    await rabbitmq.consume_ingest(capture)
    with TracerProvider().get_tracer("test").start_as_current_span("rest-ingest"):
        await rabbitmq.publish_telemetry(TELEMETRY, source="rest")

    message = (await wait_for(lambda: received))[0]
    assert Telemetry.model_validate_json(message.body) == TELEMETRY
    assert message.headers[SOURCE_HEADER] == "rest"
    assert "traceparent" in message.headers
    assert message.delivery_mode.value == 2  # persistent


async def test_live_updates_fan_out_to_every_api_instance(rabbitmq, rabbit_settings):
    other_api = RabbitMQ(rabbit_settings)
    await other_api.connect()
    first, second = [], []
    await rabbitmq.consume_live(collect_into(first))
    await other_api.consume_live(collect_into(second))

    await rabbitmq.publish_live_telemetry(TELEMETRY)
    await wait_for(lambda: first and second)
    assert first == second == [TELEMETRY]
    await other_api.close()


async def test_worker_persists_and_publishes_live(rabbitmq, mongo_db):
    live = []
    await rabbitmq.consume_live(collect_into(live))
    await rabbitmq.consume_ingest(partial(handle_message, rabbitmq=rabbitmq))

    await rabbitmq.publish_telemetry(TELEMETRY, source="rest")

    stored = await wait_for(lambda: mongo_db[mongo.TELEMETRY].find_one({"flight_id": "ITEST1"}))
    assert stored["callsign"] == "ITEST" and stored["altitude"] == 3000
    assert (await wait_for(lambda: live))[0] == TELEMETRY


async def test_worker_drops_invalid_message_and_keeps_consuming(rabbitmq, mongo_db):
    await rabbitmq.consume_ingest(partial(handle_message, rabbitmq=rabbitmq))
    await rabbitmq.publish_ingest(b'{"flight_id": "broken"}')
    await rabbitmq.publish_telemetry(TELEMETRY, source="rest")

    await wait_for(lambda: mongo_db[mongo.TELEMETRY].find_one({"flight_id": "ITEST1"}))
    assert await mongo_db[mongo.TELEMETRY].count_documents({}) == 1
