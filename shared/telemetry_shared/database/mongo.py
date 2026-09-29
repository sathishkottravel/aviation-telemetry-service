from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from telemetry_shared.config import get_settings

AIRPORTS = "airports"
WAYPOINTS = "waypoints"
FLIGHTS = "flights"
TELEMETRY = "telemetry"

_client: AsyncMongoClient | None = None


def connect() -> None:
    """Create the client. The driver connects lazily, so this does not block on an unreachable server."""
    global _client
    if _client is None:
        _client = AsyncMongoClient(get_settings().mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=5000)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def get_db() -> AsyncDatabase:
    if _client is None:
        raise RuntimeError("MongoDB client is not initialised; call connect() first")
    return _client[get_settings().mongodb_db]


async def ensure_indexes() -> None:
    """Create indexes if missing. Safe to call repeatedly."""
    db = get_db()
    await db[AIRPORTS].create_index("icao", unique=True)
    await db[WAYPOINTS].create_index("ident", unique=True)
    await db[FLIGHTS].create_index("flight_id", unique=True)
    # Serves telemetryHistory: equality on flight_id, range + sort on timestamp.
    await db[TELEMETRY].create_index([("flight_id", ASCENDING), ("timestamp", ASCENDING)])
