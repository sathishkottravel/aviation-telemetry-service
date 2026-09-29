from telemetry_shared.database.mongo import AIRPORTS, FLIGHTS, WAYPOINTS, get_db
from telemetry_shared.models import Airport, Flight, Waypoint


async def get_flight(flight_id: str) -> Flight | None:
    doc = await get_db()[FLIGHTS].find_one({"flight_id": flight_id})
    return Flight.model_validate(doc) if doc else None


async def list_airports() -> list[Airport]:
    return [Airport.model_validate(doc) async for doc in get_db()[AIRPORTS].find().sort("icao", 1)]


async def list_waypoints() -> list[Waypoint]:
    return [Waypoint.model_validate(doc) async for doc in get_db()[WAYPOINTS].find().sort("ident", 1)]


async def get_route(flight_id: str) -> list[Waypoint]:
    """Waypoints of the flight's planned route, in flight order."""
    flight = await get_flight(flight_id)
    if flight is None:
        return []
    cursor = get_db()[WAYPOINTS].find({"ident": {"$in": flight.route}})
    by_ident = {doc["ident"]: doc async for doc in cursor}
    return [Waypoint.model_validate(by_ident[ident]) for ident in flight.route if ident in by_ident]
