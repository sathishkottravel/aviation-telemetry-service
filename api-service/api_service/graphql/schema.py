from collections.abc import AsyncGenerator
from datetime import datetime

import strawberry

from api_service.graphql.types import Airport, Flight, Telemetry, Waypoint
from api_service.services import navigation_service, telemetry_service
from api_service.services.live_broadcaster import broadcaster


@strawberry.type
class Query:
    @strawberry.field
    async def flight(self, flight_id: strawberry.ID) -> Flight | None:
        flight = await navigation_service.get_flight(flight_id)
        return Flight.from_model(flight) if flight else None

    @strawberry.field
    async def airports(self) -> list[Airport]:
        return [Airport.from_model(a) for a in await navigation_service.list_airports()]

    @strawberry.field
    async def waypoints(self) -> list[Waypoint]:
        return [Waypoint.from_model(w) for w in await navigation_service.list_waypoints()]

    @strawberry.field
    async def route(self, flight_id: strawberry.ID) -> list[Waypoint]:
        return [Waypoint.from_model(w) for w in await navigation_service.get_route(flight_id)]

    @strawberry.field
    async def telemetry_history(
        self, flight_id: strawberry.ID, start: datetime | None = None, end: datetime | None = None
    ) -> list[Telemetry]:
        history = await telemetry_service.get_history(flight_id, start, end)
        return [Telemetry.from_model(t) for t in history]


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def live_telemetry(self, flight_id: strawberry.ID) -> AsyncGenerator[Telemetry, None]:
        async for telemetry in broadcaster.subscribe(flight_id):
            yield Telemetry.from_model(telemetry)


schema = strawberry.Schema(query=Query, subscription=Subscription)
