from telemetry_shared.database.mongo import TELEMETRY, get_db
from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_shared.models import Telemetry


def normalize(body: bytes) -> Telemetry:
    """Parse and validate a raw message.

    TODO: add real normalization (unit conversion, track wrap-around, timestamp sanity checks)
    once real ADS-B feeds are connected.
    """
    return Telemetry.model_validate_json(body)


async def process(body: bytes, rabbitmq: RabbitMQ) -> Telemetry:
    telemetry = normalize(body)
    # Optional ML anomaly scoring will go here, before the record is stored and broadcast.
    await get_db()[TELEMETRY].insert_one(telemetry.model_dump())
    await rabbitmq.publish_live(telemetry.model_dump_json().encode())
    return telemetry
