from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration shared by both services, read from environment variables or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "aviation"

    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    rabbitmq_ingest_exchange: str = "telemetry.ingest"
    rabbitmq_ingest_queue: str = "telemetry.ingest"
    rabbitmq_live_exchange: str = "telemetry.live"


@lru_cache
def get_settings() -> Settings:
    return Settings()
