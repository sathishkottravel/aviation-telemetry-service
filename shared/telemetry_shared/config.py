from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration shared by both services, read from environment variables or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "aviation"
    telemetry_ttl_days: float = 1.0
    """MongoDB deletes telemetry older than this automatically (TTL index). 0 disables expiry."""

    admin_token: str = ""
    """Required in the X-Admin-Token header for admin endpoints (telemetry pruning). Empty disables them."""

    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    rabbitmq_ingest_exchange: str = "telemetry.ingest"
    rabbitmq_ingest_queue: str = "telemetry.ingest"
    rabbitmq_live_exchange: str = "telemetry.live"

    otel_enabled: bool = False
    otel_service_name: str | None = None
    """Overrides the service's default name (flight-telemetry-api / -worker / -producer)."""
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_exporter_otlp_protocol: Literal["grpc", "http/protobuf"] = "grpc"
    """grpc for Jaeger/Honeycomb-style endpoints; http/protobuf for HTTP-only gateways such as Grafana Cloud."""

    producer_url: str = "http://localhost:8001"
    """Where the API reaches the ADS-B producer (for listing trackable aircraft)."""

    adsb_base_url: str = "https://api.adsb.lol"
    adsb_poll_interval: float = 5.0
    adsb_latitude: float = 59.3
    adsb_longitude: float = 18.0
    adsb_radius_nm: float = 100.0
    adsb_request_timeout: float = 10.0
    # ADSB.lol rejects generic User-Agents (403) and asks for contact info.
    adsb_user_agent: str = "aviation-telemetry-service/0.1 (+https://github.com/sathishkottravel/aviation-telemetry-service)"
    adsb_producer_aircraft: str = ""
    """Comma-separated ICAO hex codes or callsigns the producer tracks from startup."""

    @property
    def adsb_producer_aircraft_ids(self) -> list[str]:
        return [a.strip().lower() for a in self.adsb_producer_aircraft.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
