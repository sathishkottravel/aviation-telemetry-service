import asyncio
import contextlib
import logging
import os
import signal
from functools import partial

from pymongo.errors import PyMongoError

from telemetry_shared.config import get_settings
from telemetry_shared.database import mongo
from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_shared.observability.tracing import setup_tracing, shutdown_tracing
from telemetry_worker.services.health_server import start_health_server
from telemetry_worker.workers.telemetry_consumer import handle_message

logger = logging.getLogger("telemetry_worker")

async def wait_for_shutdown() -> None:
    """Block until SIGTERM (docker stop). Ctrl+C is handled by asyncio.run cancelling the main task."""
    stop = asyncio.Event()
    # add_signal_handler is not available on Windows; Ctrl+C still works there.
    with contextlib.suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    await stop.wait()


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Before the MongoDB client exists: the pymongo instrumentation hooks into clients created afterwards.
    setup_tracing("flight-telemetry-worker")

    rabbitmq = RabbitMQ(settings)
    # Hosts such as Render run the worker as a web service and expect it to listen on $PORT soon after start.
    port = os.environ.get("PORT")
    health = (
        await start_health_server(int(port), lambda: rabbitmq.is_connected, settings.cors_origin_list) if port else None
    )

    mongo.connect()
    try:
        await mongo.ensure_indexes()
    except PyMongoError as exc:
        # The worker is the telemetry writer; it can still start and the index is created on a later run.
        logger.warning("Could not ensure MongoDB indexes: %s", exc)
    try:
        await rabbitmq.connect_with_retry()
        await rabbitmq.consume_ingest(partial(handle_message, rabbitmq=rabbitmq))
        logger.info("Telemetry worker consuming from %s", settings.rabbitmq_ingest_queue)
        await wait_for_shutdown()
    finally:
        if health is not None:
            health.close()
        await rabbitmq.close()
        await mongo.close()
        shutdown_tracing()
        logger.info("Telemetry worker stopped")


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
