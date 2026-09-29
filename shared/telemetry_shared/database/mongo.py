from pymongo import AsyncMongoClient
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
