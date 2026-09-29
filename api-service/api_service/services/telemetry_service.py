from datetime import datetime

from aio_pika.abc import AbstractIncomingMessage
from opentelemetry.trace import SpanKind

from api_service.services.live_broadcaster import broadcaster
from telemetry_shared.database.mongo import TELEMETRY, get_db
from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_shared.config import get_settings
from telemetry_shared.models import Telemetry
from telemetry_shared.observability.tracing import (
    extract_context,
    messaging_attributes,
    telemetry_attributes,
    tracer,
)

REST_SOURCE = "rest"


class IngestUnavailableError(Exception):
    """RabbitMQ is not connected, so telemetry cannot be accepted."""


async def submit(telemetry: Telemetry, rabbitmq: RabbitMQ) -> None:
    """Queue telemetry for the worker. Persistence and live fan-out happen after the worker processes it."""
    with tracer.start_as_current_span("telemetry.submit", attributes=telemetry_attributes(telemetry, REST_SOURCE)):
        if not rabbitmq.is_connected:
            raise IngestUnavailableError
        await rabbitmq.publish_telemetry(telemetry, source=REST_SOURCE)


async def get_history(
    flight_id: str, start: datetime | None = None, end: datetime | None = None
) -> list[Telemetry]:
    query: dict = {"flight_id": flight_id}
    time_range = {}
    if start is not None:
        time_range["$gte"] = start
    if end is not None:
        time_range["$lte"] = end
    if time_range:
        query["timestamp"] = time_range

    cursor = get_db()[TELEMETRY].find(query).sort("timestamp", 1)
    return [Telemetry.model_validate(doc) async for doc in cursor]


async def prune(flight_id: str | None = None, before: datetime | None = None) -> int:
    """Delete telemetry matching the filters; returns how many documents were removed."""
    query: dict = {}
    if flight_id is not None:
        query["flight_id"] = flight_id
    if before is not None:
        query["timestamp"] = {"$lt": before}
    if not query:
        raise ValueError("prune needs flight_id and/or before; refusing to delete all telemetry")
    result = await get_db()[TELEMETRY].delete_many(query)
    return result.deleted_count


async def handle_live_message(message: AbstractIncomingMessage) -> None:
    """Consumer for the live exchange: hand each processed record to in-memory GraphQL subscribers.

    Continues the trace started by the ingest publisher, so it is the last hop of the pipeline trace.
    """
    attributes = messaging_attributes(get_settings().rabbitmq_live_exchange, "process")
    with tracer.start_as_current_span(
        "graphql.live_update", context=extract_context(message.headers), kind=SpanKind.CONSUMER, attributes=attributes
    ) as span:
        telemetry = Telemetry.model_validate_json(message.body)
        span.set_attributes(telemetry_attributes(telemetry))
        span.set_attribute("graphql.subscribers", broadcaster.publish(telemetry))
