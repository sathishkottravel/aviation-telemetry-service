"""Worker processing with MongoDB and RabbitMQ replaced by fakes."""

import contextlib
import json

import pytest
from pydantic import ValidationError

from telemetry_worker.services import processing_service
from telemetry_worker.workers import telemetry_consumer

RECORD = {"flight_id": "SAS123", "timestamp": "2026-09-29T10:00:00Z", "latitude": 59.35, "longitude": 17.94,
          "altitude": 3500, "ground_speed": 210, "track": 45, "vertical_rate": 1200, "callsign": "SAS123"}


class FakeCollection:
    def __init__(self):
        self.inserted = []

    async def insert_one(self, document):
        self.inserted.append(document)


class FakeRabbit:
    def __init__(self):
        self.live = []

    async def publish_live_telemetry(self, telemetry):
        self.live.append(telemetry)


@pytest.fixture
def telemetry_collection(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(processing_service, "get_db", lambda: {processing_service.TELEMETRY: collection})
    return collection


def test_normalize_validates():
    assert processing_service.normalize(json.dumps(RECORD).encode()).flight_id == "SAS123"
    with pytest.raises(ValidationError):
        processing_service.normalize(json.dumps({**RECORD, "latitude": 123}).encode())


async def test_process_persists_then_publishes_live(telemetry_collection):
    rabbit = FakeRabbit()
    telemetry = await processing_service.process(json.dumps(RECORD).encode(), rabbit)
    stored = telemetry_collection.inserted[0]
    assert stored["flight_id"] == "SAS123" and stored["timestamp"].year == 2026  # stored as a datetime
    assert rabbit.live == [telemetry]


class FakeMessage:
    """Enough of aio-pika's IncomingMessage for the consumer: body, headers, routing key and process()."""

    def __init__(self, body: bytes, headers=None):
        self.body, self.headers, self.routing_key = body, headers or {}, "telemetry.raw"
        self.outcome = None

    @contextlib.asynccontextmanager
    async def process(self, requeue=False):
        try:
            yield
        except Exception:
            self.outcome = "rejected"
            raise
        self.outcome = "acked"


async def test_consumer_acks_valid_messages(telemetry_collection):
    message = FakeMessage(json.dumps(RECORD).encode(), {"telemetry-source": b"adsb"})
    await telemetry_consumer.handle_message(message, FakeRabbit())
    assert message.outcome == "acked"
    assert len(telemetry_collection.inserted) == 1


async def test_consumer_drops_invalid_messages_without_raising(telemetry_collection):
    message = FakeMessage(b'{"flight_id": "broken"}')
    await telemetry_consumer.handle_message(message, FakeRabbit())
    assert message.outcome == "acked"  # dropped: acked so it is not redelivered forever
    assert telemetry_collection.inserted == []


async def test_consumer_rejects_on_storage_failure(monkeypatch):
    class BrokenCollection:
        async def insert_one(self, document):
            raise RuntimeError("mongo down")

    monkeypatch.setattr(processing_service, "get_db", lambda: {processing_service.TELEMETRY: BrokenCollection()})
    message = FakeMessage(json.dumps(RECORD).encode())
    with pytest.raises(RuntimeError):
        await telemetry_consumer.handle_message(message, FakeRabbit())
    assert message.outcome == "rejected"
