import logging

from aio_pika.abc import AbstractIncomingMessage
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import ValidationError

from telemetry_shared.config import get_settings
from telemetry_shared.messaging.rabbitmq import SOURCE_HEADER, RabbitMQ
from telemetry_shared.observability.tracing import extract_context, messaging_attributes, tracer
from telemetry_worker.services import processing_service

logger = logging.getLogger(__name__)


async def handle_message(message: AbstractIncomingMessage, rabbitmq: RabbitMQ) -> None:
    """Ack each message after processing it. Messages that fail are dropped, not requeued, to avoid redelivery loops.

    The span continues the publisher's trace using the context carried in the message headers.
    """
    queue = get_settings().rabbitmq_ingest_queue
    attributes = messaging_attributes(queue, "process", message.routing_key)
    if source := (message.headers or {}).get(SOURCE_HEADER):
        attributes["telemetry.source"] = source.decode() if isinstance(source, bytes) else str(source)

    with tracer.start_as_current_span(
        f"{queue} process", context=extract_context(message.headers), kind=SpanKind.CONSUMER, attributes=attributes
    ) as span:
        async with message.process(requeue=False):
            try:
                telemetry = await processing_service.process(message.body, rabbitmq)
            except ValidationError as exc:
                logger.warning("Dropping invalid telemetry message: %s", exc)
                span.set_status(Status(StatusCode.ERROR, "invalid telemetry dropped"))
                return
            logger.debug("Processed telemetry for %s at %s", telemetry.flight_id, telemetry.timestamp)
