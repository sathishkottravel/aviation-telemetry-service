import asyncio
import contextlib
import logging
import signal
from functools import partial

from pymongo.errors import PyMongoError

from telemetry_shared.config import get_settings
from telemetry_shared.database import mongo
from telemetry_shared.messaging.rabbitmq import RabbitMQ
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

    mongo.connect()
    try:
        await mongo.ensure_indexes()
    except PyMongoError as exc:
        # The worker is the telemetry writer; it can still start and the index is created on a later run.
        logger.warning("Could not ensure MongoDB indexes: %s", exc)
    rabbitmq = RabbitMQ(settings)
    try:
        await rabbitmq.connect_with_retry()
        await rabbitmq.consume_ingest(partial(handle_message, rabbitmq=rabbitmq))
        logger.info("Telemetry worker consuming from %s", settings.rabbitmq_ingest_queue)
        await wait_for_shutdown()
    finally:
        await rabbitmq.close()
        await mongo.close()
        logger.info("Telemetry worker stopped")


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
