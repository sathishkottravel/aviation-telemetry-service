from datetime import UTC, datetime

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HttpExporter
from opentelemetry.sdk.trace import TracerProvider

from telemetry_shared.config import get_settings
from telemetry_shared.models import Telemetry
from telemetry_shared.observability.tracing import (
    _build_exporter,
    extract_context,
    inject_headers,
    setup_tracing,
    telemetry_attributes,
)


class TestExporterSelection:
    def test_grpc_is_the_default(self, set_settings):
        set_settings(otel_exporter_otlp_endpoint="http://jaeger:4317")
        exporter, endpoint = _build_exporter(get_settings())
        assert isinstance(exporter, GrpcExporter)
        assert endpoint == "http://jaeger:4317"

    @pytest.mark.parametrize("configured", [
        "https://otlp-gateway.example/otlp",
        "https://otlp-gateway.example/otlp/",
        "https://otlp-gateway.example/otlp/v1/traces",
    ])
    def test_http_protocol_targets_the_traces_path_once(self, set_settings, configured):
        set_settings(otel_exporter_otlp_protocol="http/protobuf", otel_exporter_otlp_endpoint=configured)
        exporter, endpoint = _build_exporter(get_settings())
        assert isinstance(exporter, HttpExporter)
        assert endpoint == exporter._endpoint == "https://otlp-gateway.example/otlp/v1/traces"

    def test_http_exporter_sends_url_encoded_auth_header_from_env(self, set_settings, monkeypatch):
        # Grafana Cloud style: Basic auth, with the space URL-encoded as %20.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic%20dXNlcjp0b2tlbg==")
        set_settings(otel_exporter_otlp_protocol="http/protobuf", otel_exporter_otlp_endpoint="https://x.example/otlp")
        exporter, _ = _build_exporter(get_settings())
        headers = getattr(getattr(exporter, "_client", None), "_headers", None)
        if headers is None:
            pytest.skip("exporter internals changed; header parsing is covered by the SDK")
        assert headers["authorization"] == "Basic dXNlcjp0b2tlbg=="


def test_producer_aircraft_ids_are_trimmed_lowercased_and_skip_blanks(set_settings):
    set_settings(adsb_producer_aircraft=" 4AB563, ,SAS709 ,*")
    assert get_settings().adsb_producer_aircraft_ids == ["4ab563", "sas709", "*"]


def test_tracing_is_off_by_default_in_tests():
    assert setup_tracing("test-service") is False


def test_trace_context_round_trips_through_message_headers():
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("publish") as span:
        headers = inject_headers({"telemetry-source": "rest"})
    assert headers["telemetry-source"] == "rest"
    assert "traceparent" in headers

    # aio-pika may hand header values back as bytes.
    context = extract_context({k: v.encode() for k, v in headers.items()})
    extracted = trace.get_current_span(context).get_span_context()
    assert extracted.trace_id == span.get_span_context().trace_id


def test_extract_context_without_headers_starts_a_new_trace():
    assert not trace.get_current_span(extract_context(None)).get_span_context().is_valid


def test_telemetry_attributes_hold_ids_but_nothing_else():
    t = Telemetry(flight_id="4ab563", callsign="SAS709", timestamp=datetime(2026, 9, 29, tzinfo=UTC),
                  latitude=1, longitude=2, altitude=3, ground_speed=4, track=5, vertical_rate=6)
    assert telemetry_attributes(t, "adsb") == {
        "flight.id": "4ab563",
        "aircraft.id": "4ab563",
        "aircraft.callsign": "SAS709",
        "telemetry.timestamp": "2026-09-29T00:00:00+00:00",
        "telemetry.source": "adsb",
    }
