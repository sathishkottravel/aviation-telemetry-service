"""API token checks for HTTP (GraphQL + REST) and GraphQL subscriptions (WebSocket connection_init)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api_service.api import controllers
from api_service.api.auth import ApiTokenMiddleware, AuthenticatedGraphQLRouter
from api_service.graphql.schema import schema
from api_service.services import telemetry_service

TOKEN = "t0p-secret"
TELEMETRY = {"flight_id": "SAS123", "timestamp": "2026-09-29T10:00:00Z", "latitude": 59.35, "longitude": 17.94,
             "altitude": 3500, "ground_speed": 210, "track": 45, "vertical_rate": 1200}
QUERY = {"query": "{ __typename }"}


class FakeRabbit:
    is_connected = True

    async def publish_telemetry(self, telemetry, source):
        pass


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(controllers.router)
    app.include_router(AuthenticatedGraphQLRouter(schema), prefix="/graphql")
    app.add_middleware(ApiTokenMiddleware)
    app.state.rabbitmq = FakeRabbit()
    return TestClient(app)


@pytest.fixture
def token_required(set_settings):
    set_settings(api_token=TOKEN)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_everything_open_when_no_token_configured(client):
    assert client.post("/graphql", json=QUERY).json() == {"data": {"__typename": "Query"}}
    assert client.post("/api/telemetry", json=TELEMETRY).status_code == 202


@pytest.mark.usefixtures("token_required")
class TestHttp:
    @pytest.mark.parametrize("headers", [{}, bearer("wrong"), {"Authorization": TOKEN}, {"Authorization": "Basic x"}])
    def test_graphql_rejects_missing_or_wrong_token(self, client, headers):
        response = client.post("/graphql", json=QUERY, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_graphql_accepts_token(self, client):
        response = client.post("/graphql", json=QUERY, headers=bearer(TOKEN))
        assert response.json() == {"data": {"__typename": "Query"}}

    def test_graphql_get_operation_needs_token_but_graphiql_page_does_not(self, client):
        assert client.get("/graphql", params=QUERY).status_code == 401
        page = client.get("/graphql", headers={"Accept": "text/html"})
        assert page.status_code == 200 and "graphiql" in page.text.lower()

    def test_ingest_needs_token(self, client):
        assert client.post("/api/telemetry", json=TELEMETRY).status_code == 401
        assert client.post("/api/telemetry", json=TELEMETRY, headers=bearer(TOKEN)).status_code == 202

    def test_health_and_docs_stay_open(self, client):
        assert client.get("/health").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200

    def test_prune_still_uses_admin_token_not_api_token(self, client, set_settings, monkeypatch):
        set_settings(api_token=TOKEN, admin_token="admin-secret")

        async def fake_prune(flight_id=None, before=None):
            return 0

        monkeypatch.setattr(telemetry_service, "prune", fake_prune)
        url = "/api/telemetry?flight_id=X"
        assert client.delete(url, headers=bearer(TOKEN)).status_code == 401  # API token is not enough
        assert client.delete(url, headers={"X-Admin-Token": "admin-secret"}).status_code == 200


def connect(client, payload):
    """Open a graphql-transport-ws connection and return the server's reply to connection_init."""
    message = {"type": "connection_init"} if payload is None else {"type": "connection_init", "payload": payload}
    with client.websocket_connect("/graphql", subprotocols=["graphql-transport-ws"]) as ws:
        ws.send_json(message)
        return ws.receive_json()


@pytest.mark.usefixtures("token_required")
class TestSubscriptions:
    def test_accepts_token_in_connection_init(self, client):
        assert connect(client, {"authorization": f"Bearer {TOKEN}"})["type"] == "connection_ack"

    @pytest.mark.parametrize("payload", [None, {}, {"authorization": "Bearer wrong"}])
    def test_rejects_without_valid_token(self, client, payload):
        with pytest.raises(WebSocketDisconnect) as closed:
            connect(client, payload)
        assert closed.value.code == 4403


def test_subscriptions_open_when_no_token_configured(client):
    assert connect(client, None)["type"] == "connection_ack"
