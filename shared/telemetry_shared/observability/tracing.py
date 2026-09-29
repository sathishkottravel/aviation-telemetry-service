"""Optional OpenTelemetry tracing shared by all services.

With OTEL_ENABLED=false (the default) nothing is configured and the OpenTelemetry API hands out no-op
tracers, so the custom spans below cost next to nothing. When enabled, spans are exported over OTLP/gRPC
on a background thread; an unreachable collector only produces exporter warnings.

Never put connection strings, credentials or tokens into span attributes.
"""

import logging
from collections.abc import Mapping
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context

from telemetry_shared.config import get_settings
from telemetry_shared.models import Telemetry

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("flight-telemetry")
"""Proxy tracer: safe to use before setup_tracing() runs, and a no-op when tracing is disabled."""

EXPORT_TIMEOUT_S = 5


def setup_tracing(default_service_name: str) -> bool:
    """Configure tracing if OTEL_ENABLED is set. Call before creating MongoDB/HTTP clients. Returns whether it's on."""
    settings = get_settings()
    if not settings.otel_enabled:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = settings.otel_exporter_otlp_endpoint
        service_name = settings.otel_service_name or default_service_name
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        exporter = OTLPSpanExporter(
            endpoint=endpoint, insecure=endpoint.startswith("http://"), timeout=EXPORT_TIMEOUT_S
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # pymongo records only the command name (e.g. "insert"), never the query document or the URI.
        PymongoInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()
    except Exception:
        logger.warning("Tracing setup failed; continuing without tracing", exc_info=True)
        return False
    logger.info("Tracing enabled: service %s exporting to %s", service_name, endpoint)
    return True


def shutdown_tracing() -> None:
    """Flush pending spans. Safe to call when tracing is disabled."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


def inject_headers(headers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add the current trace context (W3C traceparent) to message headers."""
    carrier = dict(headers or {})
    propagate.inject(carrier)
    return carrier


def extract_context(headers: Mapping[str, Any] | None) -> Context:
    """Trace context from message headers; an empty context (new trace) if there is none."""
    carrier = {k: v.decode() if isinstance(v, bytes) else str(v) for k, v in (headers or {}).items()}
    return propagate.extract(carrier)


def telemetry_attributes(telemetry: Telemetry, source: str | None = None) -> dict[str, str]:
    attributes = {
        "flight.id": telemetry.flight_id,
        # For ADS-B the flight ID is the aircraft's ICAO hex; REST clients use the same field.
        "aircraft.id": telemetry.flight_id,
        "telemetry.timestamp": telemetry.timestamp.isoformat(),
    }
    if telemetry.callsign:
        attributes["aircraft.callsign"] = telemetry.callsign
    if source:
        attributes["telemetry.source"] = source
    return attributes


def messaging_attributes(destination: str, operation: str, routing_key: str | None = None) -> dict[str, str]:
    attributes = {
        "messaging.system": "rabbitmq",
        "messaging.destination": destination,
        "messaging.destination.name": destination,
        "messaging.operation": operation,
    }
    if routing_key is not None:
        attributes["messaging.rabbitmq.routing_key"] = routing_key
    return attributes
