"""Seed demo navigation data: one route from ESSP (Norrköping) to ESSA (Stockholm Arlanda) and one flight.

Airport positions are real. Waypoint idents and positions are illustrative, not navigation data.
Documents are upserted by their key, so running this again is safe.

    uv run seed-db
    docker compose run --rm worker seed-db
"""

import asyncio
import logging

from pydantic import BaseModel
from pymongo import ReplaceOne
from pymongo.asynchronous.collection import AsyncCollection

from telemetry_shared.database import mongo
from telemetry_shared.models import Airport, Flight, Waypoint

logger = logging.getLogger(__name__)

SEED_AIRPORTS = [
    Airport(icao="ESSP", name="Norrköping Kungsängen Airport", latitude=58.5863, longitude=16.2506, elevation_ft=32),
    Airport(icao="ESSA", name="Stockholm Arlanda Airport", latitude=59.6519, longitude=17.9186, elevation_ft=137),
]

SEED_WAYPOINTS = [
    Waypoint(ident="NOKPA", latitude=58.8000, longitude=16.5500, type="fix"),
    Waypoint(ident="SODRA", latitude=59.0200, longitude=16.8800, type="fix"),
    Waypoint(ident="MALEN", latitude=59.2400, longitude=17.2000, type="fix"),
    Waypoint(ident="ARNIX", latitude=59.4500, longitude=17.5500, type="fix"),
]

SEED_FLIGHTS = [
    Flight(
        flight_id="SAS123",
        callsign="SAS123",
        origin="ESSP",
        destination="ESSA",
        route=[w.ident for w in SEED_WAYPOINTS],
    ),
]


async def _upsert(collection: AsyncCollection, key: str, documents: list[BaseModel]) -> None:
    ops = [ReplaceOne({key: getattr(doc, key)}, doc.model_dump(), upsert=True) for doc in documents]
    result = await collection.bulk_write(ops)
    logger.info("%s: %d inserted, %d already present", collection.name, result.upserted_count, result.matched_count)


async def seed() -> None:
    mongo.connect()
    try:
        await mongo.ensure_indexes()
        db = mongo.get_db()
        await _upsert(db[mongo.AIRPORTS], "icao", SEED_AIRPORTS)
        await _upsert(db[mongo.WAYPOINTS], "ident", SEED_WAYPOINTS)
        await _upsert(db[mongo.FLIGHTS], "flight_id", SEED_FLIGHTS)
    finally:
        await mongo.close()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(seed())


if __name__ == "__main__":
    run()
