"""Producer tracking state and REST controllers, with the area poller's upstream replaced by a fake."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from adsb_producer.api.controllers import router
from adsb_producer.services.tracking_service import (
    AlreadyTrackingError,
    IngestUnavailableError,
    NotTrackingError,
    TrackingManager,
)
from telemetry_shared.adsb.client import AdsbSnapshot
from telemetry_shared.adsb.ingestion import AdsbAreaPoller, Area
from telemetry_shared.config import get_settings

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
AREA = [
    {"hex": "4ab563", "flight": "SAS709  ", "lat": 59.5, "lon": 17.9, "alt_baro": 7000, "dst": 30.0},
    {"hex": "4ab567", "flight": "SAS4225 ", "lat": 59.4, "lon": 18.1, "alt_baro": 6000, "dst": 12.0},
]


class FakeClient:
    def __init__(self):
        self.areas = []

    async def fetch_area(self, *args):
        self.areas.append(args)
        return AdsbSnapshot(NOW, AREA)


class FakeRabbit:
    def __init__(self):
        self.is_connected = True
        self.published = []

    async def publish_telemetry(self, telemetry, source):
        self.published.append(telemetry.flight_id)


@pytest.fixture
def rabbit():
    return FakeRabbit()


@pytest.fixture
def manager(rabbit):
    settings = get_settings()
    return TrackingManager(AdsbAreaPoller(FakeClient(), rabbit, settings), rabbit, settings)


class TestTrackingManager:
    def test_start_normalizes_ids_and_resolves_hex_immediately(self, manager):
        assert manager.start(" 4AB563 ").model_dump(include={"aircraft_id", "icao_hex", "running"}) == {
            "aircraft_id": "4ab563", "icao_hex": "4ab563", "running": True}
        assert manager.start("SAS4225").icao_hex is None  # callsigns resolve on the first poll

    def test_duplicates_and_unknown_stops_are_rejected(self, manager):
        manager.start("*")
        with pytest.raises(AlreadyTrackingError):
            manager.start("*")
        with pytest.raises(NotTrackingError):
            manager.stop("4ab563")

    def test_start_requires_rabbitmq(self, manager, rabbit):
        rabbit.is_connected = False
        with pytest.raises(IngestUnavailableError):
            manager.start("4ab563")

    async def test_poll_results_update_status(self, manager):
        manager.start("sas4225")
        manager.start("*")
        manager._record_results(await manager._poller.poll_once(manager.aircraft_ids()))
        callsign = manager.status("sas4225")
        assert (callsign.icao_hex, callsign.in_area, callsign.published_count) == ("4ab567", True, 1)
        area = manager.status("*")
        assert (area.aircraft_count, area.published_count, area.icao_hex) == (2, 2, None)

    def test_stop_returns_final_status_and_unknown_status_is_not_running(self, manager):
        manager.start("4ab563")
        assert manager.stop("4ab563").running is False
        assert manager.status("4ab563").model_dump(include={"running", "started_at"}) == {
            "running": False, "started_at": None}

    async def test_area_list_marks_tracked_aircraft(self, manager):
        manager.start("sas709")
        listing = await manager.list_area_aircraft()
        assert [(a.icao_hex, a.tracked) for a in listing.aircraft] == [("4ab567", False), ("4ab563", True)]


@pytest.fixture
def client(manager, rabbit):
    app = FastAPI()
    app.include_router(router)
    app.state.tracking = manager
    app.state.rabbitmq = rabbit
    return TestClient(app)


def test_rest_tracking_lifecycle(client):
    assert client.post("/ingestion/live/start/4AB563").status_code == 202
    assert client.post("/ingestion/live/start/4ab563").status_code == 409
    assert client.get("/ingestion/live/status/4ab563").json()["running"] is True
    assert client.post("/ingestion/live/stop/4ab563").json()["running"] is False
    assert client.post("/ingestion/live/stop/4ab563").status_code == 404


def test_rest_wildcard_path(client):
    assert client.post("/ingestion/live/start/*").json()["aircraft_id"] == "*"
    assert client.get("/ingestion/live/status/*").json()["running"] is True


def test_rest_start_without_rabbitmq(client, rabbit):
    rabbit.is_connected = False
    assert client.post("/ingestion/live/start/4ab563").status_code == 503


def test_rest_area_listing(client):
    body = client.get("/ingestion/live/aircraft").json()
    assert body["radius_nm"] == get_settings().adsb_radius_nm
    assert [a["icao_hex"] for a in body["aircraft"]] == ["4ab567", "4ab563"]


def test_producer_token_guards_ingestion_routes_but_not_health(client, set_settings):
    set_settings(producer_token="p-secret")
    assert client.get("/health").status_code == 200
    assert client.get("/ingestion/live/status/*").status_code == 401
    assert client.get("/ingestion/live/status/*", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/ingestion/live/status/*", headers={"Authorization": "Bearer p-secret"}).status_code == 200


def test_start_with_area_moves_the_poller_and_fills_missing_values_from_settings(client, manager):
    settings = get_settings()
    assert client.post("/ingestion/live/start/*?latitude=51.5&longitude=-0.1").status_code == 202
    assert manager._poller.area == Area(51.5, -0.1, settings.adsb_radius_nm)
    assert client.post("/ingestion/live/start/4ab563").status_code == 202  # no area: unchanged
    assert manager._poller.area.latitude == 51.5
    body = client.get("/ingestion/live/aircraft").json()
    assert (body["latitude"], body["longitude"]) == (51.5, -0.1)


def test_area_listing_for_another_area(client, manager):
    client.post("/ingestion/live/start/*")
    body = client.get("/ingestion/live/aircraft?radius_nm=25").json()
    assert body["radius_nm"] == 25
    assert manager._poller._client.areas[-1][2] == 25
    assert not any(a["tracked"] for a in body["aircraft"])  # '*' covers the polled area, not this one
    assert manager._poller.area.radius_nm == get_settings().adsb_radius_nm  # listing does not move polling


@pytest.mark.parametrize("query", ["latitude=91", "longitude=-181", "radius_nm=0", "radius_nm=251"])
def test_area_values_are_validated(client, query):
    assert client.get(f"/ingestion/live/aircraft?{query}").status_code == 422
    assert client.post(f"/ingestion/live/start/4ab563?{query}").status_code == 422


def test_docs_offer_bearer_auth_for_ingestion_routes_only(client):
    spec = client.app.openapi()
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert spec["paths"]["/ingestion/live/aircraft"]["get"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in spec["paths"]["/health"]["get"]


def test_cors_headers_for_allowed_origin(set_settings, rabbit, manager):
    from adsb_producer.api.cors import add_cors

    set_settings(cors_origins="https://sathishkottravel.github.io")
    app = FastAPI()
    app.include_router(router)
    add_cors(app)
    app.state.tracking, app.state.rabbitmq = manager, rabbit
    response = TestClient(app).get("/health", headers={"Origin": "https://sathishkottravel.github.io"})
    assert response.headers["access-control-allow-origin"] == "https://sathishkottravel.github.io"
