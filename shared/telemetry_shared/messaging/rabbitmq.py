"""RabbitMQ topology shared by all services.

    telemetry.ingest (direct) --telemetry.raw--> queue telemetry.ingest --> worker
    telemetry.live   (fanout) --> one temporary queue per API process   --> live subscribers
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage, AbstractRobustConnection

from telemetry_shared.config import Settings
from telemetry_shared.models import Telemetry

logger = logging.getLogger(__name__)

MessageHandler = Callable[[AbstractIncomingMessage], Awaitable[None]]

INGEST_ROUTING_KEY = "telemetry.raw"
# Live positions are only useful for a few seconds; drop anything older instead of replaying it.
LIVE_MESSAGE_TTL_MS = 5000
MAX_RETRY_DELAY_S = 30


class RabbitMQ:
    """One robust connection plus the ingest and live exchanges."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._ingest_exchange: AbstractExchange | None = None
        self._live_exchange: AbstractExchange | None = None

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed

    async def connect(self, timeout: float = 5.0) -> None:
        self._connection = await aio_pika.connect_robust(self._settings.rabbitmq_url, timeout=timeout)
        try:
            await self._declare_topology()
        except Exception:
            # Don't leave a half-initialised connection behind for the caller's retry.
            await self.close()
            raise

    async def connect_with_retry(self) -> None:
        """Keep trying to connect, backing off up to MAX_RETRY_DELAY_S between attempts."""
        delay = 1
        while True:
            try:
                await self.connect()
                return
            except Exception as exc:
                logger.warning("RabbitMQ not reachable (%s); retrying in %ss", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY_S)

    async def _declare_topology(self) -> None:
        self._channel = await self._require(self._connection).channel()
        await self._channel.set_qos(prefetch_count=50)

        self._ingest_exchange = await self._channel.declare_exchange(
            self._settings.rabbitmq_ingest_exchange, aio_pika.ExchangeType.DIRECT, durable=True
        )
        self._live_exchange = await self._channel.declare_exchange(
            self._settings.rabbitmq_live_exchange, aio_pika.ExchangeType.FANOUT, durable=True
        )
        # Declared by both services so ingest messages are kept even if the worker has not started yet.
        ingest_queue = await self._channel.declare_queue(self._settings.rabbitmq_ingest_queue, durable=True)
        await ingest_queue.bind(self._ingest_exchange, routing_key=INGEST_ROUTING_KEY)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def publish_ingest(self, body: bytes) -> None:
        message = aio_pika.Message(
            body, content_type="application/json", delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        )
        await self._require(self._ingest_exchange).publish(message, routing_key=INGEST_ROUTING_KEY)

    async def publish_telemetry(self, telemetry: Telemetry) -> None:
        """The single path every telemetry source (REST ingest, ADS-B producer) uses to enter the pipeline."""
        await self.publish_ingest(telemetry.model_dump_json().encode())

    async def publish_live(self, body: bytes) -> None:
        message = aio_pika.Message(
            body, content_type="application/json", delivery_mode=aio_pika.DeliveryMode.NOT_PERSISTENT
        )
        await self._require(self._live_exchange).publish(message, routing_key="")

    async def consume_ingest(self, handler: MessageHandler) -> None:
        """Worker side: consume the durable ingest queue. The handler must ack or reject each message."""
        queue = await self._require(self._channel).get_queue(self._settings.rabbitmq_ingest_queue)
        await queue.consume(handler)

    async def consume_live(self, handler: MessageHandler) -> None:
        """API side: bind a private, auto-deleted queue to the live fanout exchange."""
        queue = await self._require(self._channel).declare_queue(
            exclusive=True, auto_delete=True, arguments={"x-message-ttl": LIVE_MESSAGE_TTL_MS}
        )
        await queue.bind(self._require(self._live_exchange))
        await queue.consume(handler, no_ack=True)

    @staticmethod
    def _require[T](value: T | None) -> T:
        if value is None:
            raise RuntimeError("RabbitMQ is not connected; call connect() first")
        return value
