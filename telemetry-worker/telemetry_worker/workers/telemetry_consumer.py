import logging

from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError

from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_worker.services import processing_service

logger = logging.getLogger(__name__)


async def handle_message(message: AbstractIncomingMessage, rabbitmq: RabbitMQ) -> None:
    """Ack each message after processing it. Messages that fail are dropped, not requeued, to avoid redelivery loops."""
    async with message.process(requeue=False):
        try:
            telemetry = await processing_service.process(message.body, rabbitmq)
        except ValidationError as exc:
            logger.warning("Dropping invalid telemetry message: %s", exc)
            return
        logger.debug("Processed telemetry for %s at %s", telemetry.flight_id, telemetry.timestamp)
