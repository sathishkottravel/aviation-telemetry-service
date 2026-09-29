"""GraphQL schema tests with the service layer stubbed out (no MongoDB, RabbitMQ or producer needed)."""

from datetime import UTC, datetime

import pytest

from api_service.graphql.schema import schema
from api_service.services import adsb_service, navigation_service, telemetry_service
from api_service.services.adsb_service import AlreadyTrackingError, NotTrackingError, ProducerUnavailableError
from telemetry_shared.models import (
    Airport,
    AreaAircraft,
    AreaAircraftList,
    Flight,
    Telemetry,
    TrackingStatus,
    Waypoint,
)

T0 = datetime(2026, 9, 29, 12, tzinfo=UTC)
WAYPOINTS = [Waypoint(ident="NOKPA", latitude=58.8, longitude=16.55, type="fix")]
FLIGHT = Flight(flight_id="SAS123", callsign="SAS123", origin="ESSP", destination="ESSA", route=["NOKPA"])


def stub(monkeypatch, module, name, result):
    async def fake(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result(*args, **kwargs) if callable(result) else result

    monkeypatch.setattr(module, name, fake)


async def run(query: str):
    return await schema.execute(query)


async def test_navigation_queries(monkeypatch):
    stub(monkeypatch, navigation_service, "get_flight", FLIGHT)
    stub(monkeypatch, navigation_service, "list_airports",
         [Airport(icao="ESSA", name="Stockholm Arlanda Airport", latitude=59.65, longitude=17.92)])
    stub(monkeypatch, navigation_service, "list_waypoints", WAYPOINTS)
    stub(monkeypatch, navigation_service, "get_route", WAYPOINTS)
    result = await run('''{
        flight(flightId: "SAS123") { flightId callsign origin destination route }
        airports { icao name }
        waypoints { ident type }
        route(flightId: "SAS123") { ident latitude longitude }
    }''')
    assert result.errors is None
    assert result.data["flight"] == {"flightId": "SAS123", "callsign": "SAS123", "origin": "ESSP",
                                     "destination": "ESSA", "route": ["NOKPA"]}
    assert result.data["airports"] == [{"icao": "ESSA", "name": "Stockholm Arlanda Airport"}]
    assert result.data["waypoints"] == [{"ident": "NOKPA", "type": "fix"}]
    assert result.data["route"] == [{"ident": "NOKPA", "latitude": 58.8, "longitude": 16.55}]


async def test_unknown_flight_is_null(monkeypatch):
    stub(monkeypatch, navigation_service, "get_flight", None)
    assert (await run('{ flight(flightId: "NOPE") { flightId } }')).data == {"flight": None}


async def test_telemetry_history_passes_time_range(monkeypatch):
    calls = []
    point = Telemetry(flight_id="SAS123", callsign="SAS123", timestamp=T0, latitude=59, longitude=18,
                      altitude=3000, ground_speed=200, track=45, vertical_rate=0)
    stub(monkeypatch, telemetry_service, "get_history", lambda *a: calls.append(a) or [point])
    result = await run('''{ telemetryHistory(flightId: "SAS123", start: "2026-09-29T11:00:00Z",
                                             end: "2026-09-29T13:00:00Z") { flightId callsign altitude groundSpeed } }''')
    assert result.data["telemetryHistory"] == [
        {"flightId": "SAS123", "callsign": "SAS123", "altitude": 3000.0, "groundSpeed": 200.0}]
    flight_id, start, end = calls[0]
    assert (flight_id, start.hour, end.hour) == ("SAS123", 11, 13)


async def test_trackable_aircraft(monkeypatch):
    stub(monkeypatch, adsb_service, "list_trackable_aircraft", AreaAircraftList(
        fetched_at=T0, latitude=59.3, longitude=18.0, radius_nm=100,
        aircraft=[AreaAircraft(icao_hex="4ab563", callsign="SAS709", latitude=59.5, longitude=17.9, altitude=7000,
                               ground_speed=250, track=10, distance_nm=12.5, tracked=True)]))
    result = await run("{ trackableAircraft { radiusNm aircraft { icaoHex callsign distanceNm tracked } } }")
    assert result.data["trackableAircraft"] == {"radiusNm": 100.0, "aircraft": [
        {"icaoHex": "4ab563", "callsign": "SAS709", "distanceNm": 12.5, "tracked": True}]}


async def test_tracking_mutations_and_status(monkeypatch):
    status = TrackingStatus(aircraft_id="sas709", icao_hex="4ab563", running=True, aircraft_count=1)
    stub(monkeypatch, adsb_service, "start_tracking", status)
    stub(monkeypatch, adsb_service, "tracking_status", status)
    stub(monkeypatch, adsb_service, "stop_tracking", status.model_copy(update={"running": False}))
    start = await run('mutation { startTracking(aircraftId: "SAS709") { aircraftId icaoHex running } }')
    assert start.data["startTracking"] == {"aircraftId": "sas709", "icaoHex": "4ab563", "running": True}
    assert (await run('{ trackingStatus(aircraftId: "sas709") { aircraftCount } }')).data == {
        "trackingStatus": {"aircraftCount": 1}}
    assert (await run('mutation { stopTracking(aircraftId: "sas709") { running } }')).data == {
        "stopTracking": {"running": False}}


@pytest.mark.parametrize("operation,service_fn,error,code", [
    ('mutation { startTracking(aircraftId: "x") { running } }', "start_tracking",
     AlreadyTrackingError("Already tracking x"), "ALREADY_TRACKING"),
    ('mutation { stopTracking(aircraftId: "x") { running } }', "stop_tracking",
     NotTrackingError("Not tracking x"), "NOT_TRACKING"),
    ('{ trackingStatus(aircraftId: "x") { running } }', "tracking_status",
     ProducerUnavailableError("cannot reach producer"), "PRODUCER_UNAVAILABLE"),
    ("{ trackableAircraft { fetchedAt } }", "list_trackable_aircraft",
     ProducerUnavailableError("cannot reach producer"), "PRODUCER_UNAVAILABLE"),
])
async def test_producer_errors_carry_codes(monkeypatch, operation, service_fn, error, code):
    stub(monkeypatch, adsb_service, service_fn, error)
    result = await run(operation)
    assert result.errors[0].extensions == {"code": code}


async def test_live_telemetry_subscription_streams_published_positions():
    import asyncio

    from api_service.services.live_broadcaster import broadcaster

    stream = await schema.subscribe('subscription { liveTelemetry(flightId: "SAS123") { flightId altitude } }')
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    broadcaster.publish(Telemetry(flight_id="SAS123", timestamp=T0, latitude=0, longitude=0, altitude=1234,
                                  ground_speed=0, track=0, vertical_rate=0))
    result = await asyncio.wait_for(first, 2)
    assert result.data == {"liveTelemetry": {"flightId": "SAS123", "altitude": 1234.0}}
    await stream.aclose()
