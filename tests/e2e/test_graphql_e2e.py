"""End-to-end GraphQL tests against the running stack (docker compose up).

    uv run pytest -m e2e
"""

import asyncio

import pytest

from .conftest import ADMIN_TOKEN, telemetry_payload


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}

pytestmark = pytest.mark.e2e


async def test_navigation_queries(gql, seeded):
    data = await gql.data('''{
        flight(flightId: "SAS123") { flightId callsign origin destination route }
        route(flightId: "SAS123") { ident latitude longitude type }
        airports { icao name latitude longitude }
        waypoints { ident }
    }''')
    assert data["flight"] == {"flightId": "SAS123", "callsign": "SAS123", "origin": "ESSP", "destination": "ESSA",
                              "route": ["NOKPA", "SODRA", "MALEN", "ARNIX"]}
    assert [w["ident"] for w in data["route"]] == data["flight"]["route"]
    assert {"ESSA", "ESSP"} <= {a["icao"] for a in data["airports"]}
    assert {"NOKPA", "ARNIX"} <= {w["ident"] for w in data["waypoints"]}


async def test_unknown_flight_is_null(gql):
    assert await gql.data('{ flight(flightId: "NO-SUCH-FLIGHT") { flightId } }') == {"flight": None}


async def test_ingest_reaches_live_subscription_and_history(api, gql, flight_id):
    subscription = "subscription($id: ID!) { liveTelemetry(flightId: $id) { flightId callsign altitude } }"
    async with gql.subscribe(subscription, {"id": flight_id}) as next_data:
        response = await api.post("/api/telemetry", json=telemetry_payload(flight_id))
        assert response.status_code == 202
        # A single-flight subscription gets a one-element list.
        assert await next_data() == {"liveTelemetry": [{"flightId": flight_id, "callsign": "E2ETEST", "altitude": 3500.0}]}

    history = await gql.data("query($id: ID!) { telemetryHistory(flightId: $id) { flightId altitude timestamp } }",
                             {"id": flight_id})
    assert history["telemetryHistory"] == [{"flightId": flight_id, "altitude": 3500.0,
                                            "timestamp": "2026-09-29T12:00:00+00:00"}]


async def test_history_time_window_and_order(api, gql, flight_id):
    for minute in (20, 0, 10):
        payload = telemetry_payload(flight_id, timestamp=f"2026-09-29T12:{minute:02d}:00Z", altitude=1000 + minute)
        assert (await api.post("/api/telemetry", json=payload)).status_code == 202

    query = "query($id: ID!, $s: DateTime, $e: DateTime) { telemetryHistory(flightId: $id, start: $s, end: $e) { altitude } }"
    for _ in range(50):  # the worker persists asynchronously
        full = (await gql.data(query, {"id": flight_id}))["telemetryHistory"]
        if len(full) == 3:
            break
        await asyncio.sleep(0.1)
    assert [p["altitude"] for p in full] == [1000, 1010, 1020]
    window = await gql.data(query, {"id": flight_id, "s": "2026-09-29T12:05:00Z", "e": "2026-09-29T12:10:00Z"})
    assert window["telemetryHistory"] == [{"altitude": 1010.0}]


async def test_wildcard_subscription_lists_every_flight(api, gql, flight_id):
    second = f"{flight_id}-B"
    async with gql.subscribe("subscription { liveTelemetry(flightId: \"*\") { flightId altitude } }") as next_data:
        await api.post("/api/telemetry", json=telemetry_payload(flight_id, altitude=1000))
        await api.post("/api/telemetry", json=telemetry_payload(second, altitude=2000))
        # Each message is a snapshot of every flight's latest position; other traffic may be in it too.
        for _ in range(20):
            snapshot = {p["flightId"]: p["altitude"] for p in (await next_data())["liveTelemetry"]}
            if flight_id in snapshot and second in snapshot:
                break
        else:
            pytest.fail("wildcard snapshots never contained both test flights")
    assert (snapshot[flight_id], snapshot[second]) == (1000, 2000)
    await api.delete("/api/telemetry", params={"flight_id": second}, headers=_admin_headers())


async def test_late_subscriber_gets_current_positions_immediately(api, gql, flight_id):
    assert (await api.post("/api/telemetry", json=telemetry_payload(flight_id))).status_code == 202
    subscription = "subscription($id: ID!) { liveTelemetry(flightId: $id) { flightId } }"
    for _ in range(50):  # wait until the worker has processed it and the API has cached it
        async with gql.subscribe(subscription, {"id": flight_id}) as next_data:
            try:
                snapshot = await next_data(timeout=0.5)
            except TimeoutError:
                continue
        assert snapshot == {"liveTelemetry": [{"flightId": flight_id}]}
        return
    pytest.fail("a new subscriber never received the cached position")


async def test_tracking_lifecycle(gql):
    aircraft = "e2e0ff"  # a hex that isn't in the area: tracking works, it just never publishes
    fields = "aircraftId icaoHex running inArea publishedCount lastError"
    start = await gql.execute(f'mutation {{ startTracking(aircraftId: "{aircraft.upper()}") {{ {fields} }} }}')
    if start.get("errors") and start["errors"][0]["extensions"]["code"] == "PRODUCER_UNAVAILABLE":
        pytest.skip("producer not reachable from the API")
    try:
        assert start["data"]["startTracking"]["aircraftId"] == aircraft
        assert start["data"]["startTracking"]["icaoHex"] == aircraft  # hex IDs resolve immediately
        assert await gql.error_code(f'mutation {{ startTracking(aircraftId: "{aircraft}") {{ running }} }}') == \
            "ALREADY_TRACKING"
        status = await gql.data(f'{{ trackingStatus(aircraftId: "{aircraft}") {{ running }} }}')
        assert status == {"trackingStatus": {"running": True}}
    finally:
        stop = await gql.data(f'mutation {{ stopTracking(aircraftId: "{aircraft}") {{ running }} }}')
    assert stop == {"stopTracking": {"running": False}}
    assert await gql.error_code(f'mutation {{ stopTracking(aircraftId: "{aircraft}") {{ running }} }}') == "NOT_TRACKING"
    assert await gql.data(f'{{ trackingStatus(aircraftId: "{aircraft}") {{ running startedAt }} }}') == {
        "trackingStatus": {"running": False, "startedAt": None}}


async def test_trackable_aircraft(gql):
    body = await gql.execute("{ trackableAircraft { fetchedAt radiusNm aircraft { icaoHex callsign distanceNm tracked } } }")
    if body.get("errors"):
        # ADSB.lol may be rate limiting or unreachable before the first snapshot; the error must still be typed.
        assert body["errors"][0]["extensions"]["code"] == "PRODUCER_UNAVAILABLE"
        pytest.skip(f"no ADS-B snapshot available: {body['errors'][0]['message']}")
    area = body["data"]["trackableAircraft"]
    assert area["radiusNm"] > 0
    distances = [a["distanceNm"] for a in area["aircraft"] if a["distanceNm"] is not None]
    assert distances == sorted(distances)


async def test_invalid_query_returns_graphql_error(gql):
    body = await gql.execute("{ flight { flightId } }")  # missing required flightId argument
    assert "flightId" in body["errors"][0]["message"]
