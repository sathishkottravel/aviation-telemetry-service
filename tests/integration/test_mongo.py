from datetime import UTC, datetime, timedelta

import pytest

from api_service.services import navigation_service, telemetry_service
from telemetry_shared.database import mongo, seed
from telemetry_shared.models import Telemetry

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 29, 12, tzinfo=UTC)


def point(flight_id: str, minutes: int) -> dict:
    return Telemetry(flight_id=flight_id, timestamp=T0 + timedelta(minutes=minutes), latitude=59, longitude=18,
                     altitude=1000 + minutes, ground_speed=200, track=90, vertical_rate=0).model_dump()


async def test_indexes_including_ttl(mongo_db, set_settings):
    await mongo.ensure_indexes()
    telemetry_indexes = await mongo_db[mongo.TELEMETRY].index_information()
    assert telemetry_indexes["flight_id_1_timestamp_1"]["key"] == [("flight_id", 1), ("timestamp", 1)]
    assert telemetry_indexes[mongo.TELEMETRY_TTL_INDEX]["expireAfterSeconds"] == 86400
    assert (await mongo_db[mongo.AIRPORTS].index_information())["icao_1"]["unique"] is True

    set_settings(telemetry_ttl_days=2)
    await mongo.ensure_indexes()
    ttl = (await mongo_db[mongo.TELEMETRY].index_information())[mongo.TELEMETRY_TTL_INDEX]
    assert ttl["expireAfterSeconds"] == 172800  # changed in place via collMod

    set_settings(telemetry_ttl_days=0)
    await mongo.ensure_indexes()
    assert mongo.TELEMETRY_TTL_INDEX not in await mongo_db[mongo.TELEMETRY].index_information()


@pytest.mark.usefixtures("mongo_db")
async def test_seed_is_idempotent_and_route_is_ordered():
    await mongo.close()  # seed() opens and closes its own client
    await seed.seed()
    await seed.seed()
    mongo.connect()
    db = mongo.get_db()
    assert await db[mongo.AIRPORTS].count_documents({}) == 2
    assert await db[mongo.FLIGHTS].count_documents({}) == 1
    flight = await navigation_service.get_flight("SAS123")
    route = await navigation_service.get_route("SAS123")
    assert (flight.origin, flight.destination) == ("ESSP", "ESSA")
    assert [w.ident for w in route] == flight.route == ["NOKPA", "SODRA", "MALEN", "ARNIX"]


async def test_history_is_ordered_and_filtered_by_time(mongo_db):
    await mongo_db[mongo.TELEMETRY].insert_many([point("A", 20), point("A", 0), point("A", 10), point("B", 5)])
    history = await telemetry_service.get_history("A")
    assert [t.altitude for t in history] == [1000, 1010, 1020]
    window = await telemetry_service.get_history("A", start=T0 + timedelta(minutes=5), end=T0 + timedelta(minutes=10))
    assert [t.altitude for t in window] == [1010]


async def test_prune_by_flight_and_time(mongo_db):
    await mongo_db[mongo.TELEMETRY].insert_many([point("A", 0), point("A", 10), point("B", 0)])
    assert await telemetry_service.prune(flight_id="A", before=T0 + timedelta(minutes=5)) == 1
    assert await telemetry_service.prune(flight_id="B") == 1
    assert [t.altitude for t in await telemetry_service.get_history("A")] == [1010]
