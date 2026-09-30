"""Test-wide setup, shared by every service's tests/ folder.

Environment variables override the developer's .env, so tests never trace to Jaeger, never touch the real
database, and see predictable admin/producer settings. Individual tests adjust settings with set_settings().
"""

import os
from collections.abc import Callable

import pytest

os.environ.update(
    {
        "OTEL_ENABLED": "false",
        "MONGODB_DB": "aviation_test",
        "ADMIN_TOKEN": "",
        "ADSB_POLL_INTERVAL": "5",
        "PRODUCER_URL": "http://producer.test",
        "PRODUCER_TOKEN": "",
        "WAKE_URLS": "",
    }
)

from telemetry_shared.config import get_settings  # noqa: E402  (must follow the env setup above)


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def set_settings(monkeypatch) -> Callable[..., None]:
    """Override settings for one test, e.g. set_settings(admin_token="secret")."""

    def apply(**values: object) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key.upper(), str(value))
        get_settings.cache_clear()

    return apply
