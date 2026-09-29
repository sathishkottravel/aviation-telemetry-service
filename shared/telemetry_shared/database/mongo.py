from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from telemetry_shared.config import get_settings

AIRPORTS = "airports"
WAYPOINTS = "waypoints"
FLIGHTS = "flights"
TELEMETRY = "telemetry"

TELEMETRY_TTL_INDEX = "telemetry_ttl"

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
    await _ensure_telemetry_ttl(db, get_settings().telemetry_ttl_days)


async def _ensure_telemetry_ttl(db: AsyncDatabase, ttl_days: float) -> None:
    """Keep the TTL index on telemetry.timestamp in line with TELEMETRY_TTL_DAYS (create, change or drop).

    MongoDB's TTL monitor runs about once a minute, so expired documents disappear shortly after they expire.
    """
    collection = db[TELEMETRY]
    existing = (await collection.index_information()).get(TELEMETRY_TTL_INDEX)
    if ttl_days <= 0:
        if existing is not None:
            await collection.drop_index(TELEMETRY_TTL_INDEX)
        return

    seconds = int(ttl_days * 86400)
    if existing is None:
        await collection.create_index("timestamp", name=TELEMETRY_TTL_INDEX, expireAfterSeconds=seconds)
    elif existing.get("expireAfterSeconds") != seconds:
        # collMod changes the expiry in place; create_index would fail on the conflicting option.
        await db.command("collMod", TELEMETRY, index={"name": TELEMETRY_TTL_INDEX, "expireAfterSeconds": seconds})
