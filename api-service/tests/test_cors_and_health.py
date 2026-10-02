"""CORS (CORS_ORIGINS) and the combined GET /health/all."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_service.api import controllers
from api_service.api.auth import ApiTokenMiddleware, AuthenticatedGraphQLRouter
from api_service.api.cors import add_cors
from api_service.graphql.schema import schema
from api_service.services import adsb_service, health_service

ORIGIN = "https://sathishkottravel.github.io"


class FakeRabbit:
    is_connected = True


def make_client() -> TestClient:
    # Same order as api_service.main: token middleware first, CORS added last so it runs first.
    app = FastAPI()
    app.include_router(controllers.router)
    app.include_router(AuthenticatedGraphQLRouter(schema), prefix="/graphql")
    app.add_middleware(ApiTokenMiddleware)
    add_cors(app)
    app.state.rabbitmq = FakeRabbit()
    return TestClient(app)


class TestCors:
    def test_allowed_origin_gets_headers_and_preflight_skips_the_token_check(self, set_settings):
        set_settings(cors_origins=f" {ORIGIN}/ , http://localhost:5173", api_token="secret")
        client = make_client()
        assert client.get("/health", headers={"Origin": ORIGIN}).headers["access-control-allow-origin"] == ORIGIN
        preflight = client.options("/graphql", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"})
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == ORIGIN
        # The real request still needs the token.
        assert client.post("/graphql", json={"query": "{ __typename }"}, headers={"Origin": ORIGIN}).status_code == 401

    def test_other_origins_and_empty_setting_get_no_headers(self, set_settings):
        set_settings(cors_origins=ORIGIN)
        assert "access-control-allow-origin" not in make_client().get(
            "/health", headers={"Origin": "https://evil.example"}).headers
        set_settings(cors_origins="")
        assert "access-control-allow-origin" not in make_client().get("/health", headers={"Origin": ORIGIN}).headers


def test_cors_origin_list_is_split_trimmed_and_slash_free(set_settings):
    from telemetry_shared.config import get_settings

    set_settings(cors_origins=" https://a.test/ , ,http://localhost:5173 ")
    assert get_settings().cors_origin_list == ["https://a.test", "http://localhost:5173"]


class TestHealthAll:
    @pytest.fixture
    def worker(self, monkeypatch, set_settings):
        """Point WORKER_URL at a MockTransport; returns a setter for the worker's response."""
        set_settings(worker_url="https://worker.test")
        response = {"value": httpx.Response(200, json={"status": "ok", "rabbitmq": True})}
        real_client = httpx.AsyncClient

        def handler(request):
            assert str(request.url) == "https://worker.test/health"
            if isinstance(response["value"], Exception):
                raise response["value"]
            return response["value"]

        monkeypatch.setattr(health_service.httpx, "AsyncClient",
                            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
        return lambda r: response.update(value=r)

    def producer(self, monkeypatch, result):
        async def fake():
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(adsb_service, "health", fake)

    def test_all_ok(self, monkeypatch, worker):
        self.producer(monkeypatch, {"status": "ok", "rabbitmq": True})
        assert make_client().get("/health/all").json() == {"status": "ok", "services": {
            "api": {"status": "ok", "rabbitmq": True},
            "producer": {"status": "ok", "rabbitmq": True},
            "worker": {"status": "ok", "rabbitmq": True}}}

    def test_unreachable_services_make_it_degraded(self, monkeypatch, worker):
        self.producer(monkeypatch, httpx.ConnectError("refused"))
        worker(httpx.Response(503))
        body = make_client().get("/health/all").json()
        assert body["status"] == "degraded"
        assert body["services"]["producer"] == body["services"]["worker"] == {"status": "unreachable"}

    def test_worker_without_url_is_not_configured(self, monkeypatch, set_settings):
        set_settings(worker_url="")
        self.producer(monkeypatch, {"status": "ok", "rabbitmq": True})
        body = make_client().get("/health/all").json()
        assert body["status"] == "ok" and body["services"]["worker"] == {"status": "not_configured"}
