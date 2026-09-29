from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_service.api import controllers
from api_service.services import telemetry_service

VALID = {"flight_id": "SAS123", "timestamp": "2026-09-29T10:00:00Z", "latitude": 59.35, "longitude": 17.94,
         "altitude": 3500, "ground_speed": 210, "track": 45, "vertical_rate": 1200}


class FakeRabbit:
    def __init__(self, connected=True):
        self.is_connected = connected
        self.published = []

    async def publish_telemetry(self, telemetry, source):
        self.published.append((telemetry, source))


@pytest.fixture
def rabbit():
    return FakeRabbit()


@pytest.fixture
def client(rabbit):
    # Only the router: no lifespan, so no MongoDB/RabbitMQ connections are attempted.
    app = FastAPI()
    app.include_router(controllers.router)
    app.state.rabbitmq = rabbit
    return TestClient(app)


def test_health_reports_rabbitmq_state(client):
    assert client.get("/health").json() == {"status": "ok", "rabbitmq": True}


def test_ingest_publishes_with_rest_source(client, rabbit):
    response = client.post("/api/telemetry", json=VALID)
    assert response.status_code == 202
    telemetry, source = rabbit.published[0]
    assert (telemetry.flight_id, source) == ("SAS123", "rest")


@pytest.mark.parametrize("field,value", [("latitude", 91), ("track", 360), ("ground_speed", -1)])
def test_ingest_rejects_invalid_telemetry(client, field, value):
    assert client.post("/api/telemetry", json={**VALID, field: value}).status_code == 422


def test_ingest_returns_503_when_rabbitmq_is_down(client, rabbit):
    rabbit.is_connected = False
    assert client.post("/api/telemetry", json=VALID).status_code == 503


class TestPrune:
    @pytest.fixture
    def deletes(self, monkeypatch):
        calls = []

        async def fake_prune(flight_id=None, before=None):
            calls.append((flight_id, before))
            return 7

        monkeypatch.setattr(telemetry_service, "prune", fake_prune)
        return calls

    def test_disabled_without_admin_token(self, client, deletes):
        response = client.delete("/api/telemetry?flight_id=X", headers={"X-Admin-Token": "anything"})
        assert response.status_code == 403
        assert deletes == []

    @pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}])
    def test_requires_matching_token(self, client, deletes, set_settings, headers):
        set_settings(admin_token="s3cret")
        assert client.delete("/api/telemetry?flight_id=X", headers=headers).status_code == 401
        assert deletes == []

    @pytest.mark.parametrize("query", ["", "?older_than_hours=1&before=2026-01-01T00:00:00Z", "?older_than_hours=0"])
    def test_rejects_missing_or_conflicting_filters(self, client, deletes, set_settings, query):
        set_settings(admin_token="s3cret")
        assert client.delete(f"/api/telemetry{query}", headers={"X-Admin-Token": "s3cret"}).status_code == 422
        assert deletes == []

    def test_deletes_with_filters(self, client, deletes, set_settings):
        set_settings(admin_token="s3cret")
        response = client.delete("/api/telemetry?flight_id=X&older_than_hours=2", headers={"X-Admin-Token": "s3cret"})
        assert response.status_code == 200
        assert response.json()["deleted"] == 7
        flight_id, before = deletes[0]
        assert flight_id == "X"
        assert 1.9 * 3600 < (datetime.now(UTC) - before).total_seconds() < 2.1 * 3600


async def test_prune_service_refuses_to_delete_everything():
    with pytest.raises(ValueError):
        await telemetry_service.prune()
