"""API token checks for HTTP (GraphQL + REST) and GraphQL subscriptions (WebSocket connection_init)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api_service.api import controllers
from api_service.api.auth import ApiTokenMiddleware, AuthenticatedGraphQLRouter, add_cors
from api_service.graphql.schema import schema
from api_service.services import adsb_service, telemetry_service
from telemetry_shared.models import TrackingStatus

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


START = {"query": 'mutation { startTracking(aircraftId: "sas709") { running } }'}
PAGES = "https://sathishkottravel.github.io"


@pytest.fixture
def stub_tracking(monkeypatch):
    async def fake_start(*args, **kwargs):
        return TrackingStatus(aircraft_id="sas709", icao_hex="4ab563", running=True)

    monkeypatch.setattr(adsb_service, "start_tracking", fake_start)


@pytest.fixture
def public_read(set_settings):
    set_settings(api_token=TOKEN, public_read=True)


@pytest.mark.usefixtures("public_read", "stub_tracking")
class TestPublicRead:
    def test_queries_need_no_token(self, client):
        assert client.post("/graphql", json=QUERY).json() == {"data": {"__typename": "Query"}}
        assert client.get("/graphql", params=QUERY).status_code == 200

    def test_mutations_still_need_the_token(self, client):
        body = client.post("/graphql", json=START).json()
        assert body["data"] is None
        assert body["errors"][0]["message"] == "API token required to start or stop tracking"
        assert body["errors"][0]["extensions"] == {"code": "UNAUTHENTICATED"}
        wrong = client.post("/graphql", json=START, headers=bearer("wrong")).json()
        assert wrong["errors"][0]["extensions"] == {"code": "UNAUTHENTICATED"}

    def test_mutations_accept_the_token(self, client):
        response = client.post("/graphql", json=START, headers=bearer(TOKEN))
        assert response.json() == {"data": {"startTracking": {"running": True}}}

    def test_subscriptions_need_no_token(self, client):
        assert connect(client, None)["type"] == "connection_ack"

    def test_ingest_still_needs_the_token(self, client):
        assert client.post("/api/telemetry", json=TELEMETRY).status_code == 401


@pytest.mark.usefixtures("token_required", "stub_tracking")
def test_mutation_permission_is_redundant_but_harmless_without_public_read(client):
    assert client.post("/graphql", json=START).status_code == 401
    response = client.post("/graphql", json=START, headers=bearer(TOKEN))
    assert response.json() == {"data": {"startTracking": {"running": True}}}


@pytest.fixture
def cors_client(client):
    add_cors(client.app, [PAGES, "null"])
    return client


def preflight(client, origin):
    return client.options("/graphql", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })


@pytest.mark.usefixtures("token_required")
class TestCors:
    @pytest.mark.parametrize("origin", [PAGES, "null"])
    def test_preflight_from_allowed_origin_skips_the_token_check(self, cors_client, origin):
        response = preflight(cors_client, origin)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert "authorization" in response.headers["access-control-allow-headers"].lower()

    def test_other_origins_get_no_cors_headers(self, cors_client):
        response = preflight(cors_client, "https://evil.example")
        assert "access-control-allow-origin" not in response.headers

    def test_401_is_readable_by_the_allowed_origin(self, cors_client):
        response = cors_client.post("/graphql", json=QUERY, headers={"Origin": PAGES})
        assert response.status_code == 401
        assert response.headers["access-control-allow-origin"] == PAGES

    def test_off_by_default(self, client):
        assert preflight(client, PAGES).status_code == 401
