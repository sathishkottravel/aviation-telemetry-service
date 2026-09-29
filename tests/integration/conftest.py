"""Integration fixtures: real MongoDB and RabbitMQ (docker compose up -d mongo rabbitmq).

Isolated from a running stack: MongoDB uses the aviation_test database (dropped afterwards) and RabbitMQ
uses test.* exchanges/queues, so the stack's worker never consumes test messages. Tests are skipped
when either service is unreachable.
"""

import asyncio
import socket
from urllib.parse import urlparse

import pytest

from telemetry_shared.config import get_settings
from telemetry_shared.database import mongo
from telemetry_shared.messaging.rabbitmq import RabbitMQ

pytestmark = pytest.mark.integration


def _reachable(url: str, default_port: int) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or default_port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture
async def mongo_db(set_settings):
    set_settings(mongodb_db="aviation_test", telemetry_ttl_days=1)
    if not _reachable(get_settings().mongodb_uri, 27017):
        pytest.skip("MongoDB not reachable")
    mongo.connect()
    db = mongo.get_db()
    await db.client.drop_database("aviation_test")
    yield db
    # A test may have replaced the shared client (seed() opens and closes its own), so use the current one.
    mongo.connect()
    await mongo.get_db().client.drop_database("aviation_test")
    await mongo.close()


@pytest.fixture
async def rabbit_settings(set_settings):
    set_settings(
        rabbitmq_ingest_exchange="test.telemetry.ingest",
        rabbitmq_ingest_queue="test.telemetry.ingest",
        rabbitmq_live_exchange="test.telemetry.live",
    )
    if not _reachable(get_settings().rabbitmq_url, 5672):
        pytest.skip("RabbitMQ not reachable")
    return get_settings()


@pytest.fixture
async def rabbitmq(rabbit_settings):
    rabbit = RabbitMQ(rabbit_settings)
    await rabbit.connect()
    queue = await rabbit._channel.get_queue(rabbit_settings.rabbitmq_ingest_queue)
    await queue.purge()
    yield rabbit
    await queue.delete(if_unused=False, if_empty=False)
    await rabbit._channel.exchange_delete(rabbit_settings.rabbitmq_ingest_exchange)
    await rabbit._channel.exchange_delete(rabbit_settings.rabbitmq_live_exchange)
    await rabbit.close()


async def wait_for(predicate, timeout=5.0):
    """Poll an async or sync predicate until it is truthy."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)
