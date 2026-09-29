from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError

from telemetry_shared.database.mongo import TELEMETRY, get_db
from telemetry_shared.messaging.rabbitmq import RabbitMQ
from telemetry_shared.models import Telemetry
from telemetry_shared.observability.tracing import telemetry_attributes, tracer


def normalize(body: bytes) -> Telemetry:
    """Parse and validate a raw message.

    TODO: add real normalization (unit conversion, track wrap-around, timestamp sanity checks)
    once real ADS-B feeds are connected.
    """
    with tracer.start_as_current_span("telemetry.normalize") as span:
        try:
            telemetry = Telemetry.model_validate_json(body)
        except ValidationError as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, "validation failed"))
            raise
        span.set_attributes(telemetry_attributes(telemetry))
        return telemetry


async def process(body: bytes, rabbitmq: RabbitMQ) -> Telemetry:
    telemetry = normalize(body)
    # Tag the enclosing consume span too, so the whole worker branch is searchable by flight.id.
    trace.get_current_span().set_attributes(telemetry_attributes(telemetry))
    # Optional ML anomaly scoring will go here, before the record is stored and broadcast.
    with tracer.start_as_current_span("telemetry.persist", attributes=telemetry_attributes(telemetry)):
        await get_db()[TELEMETRY].insert_one(telemetry.model_dump())
    await rabbitmq.publish_live_telemetry(telemetry)
    return telemetry
