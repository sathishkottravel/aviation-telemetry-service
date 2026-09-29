from collections.abc import AsyncGenerator
from datetime import datetime

import strawberry
from graphql import GraphQLError

from api_service.graphql.types import Airport, Flight, Telemetry, TrackableArea, TrackingStatus, Waypoint
from api_service.services import adsb_service, navigation_service, telemetry_service
from api_service.services.adsb_service import AlreadyTrackingError, NotTrackingError, ProducerUnavailableError
from api_service.services.live_broadcaster import broadcaster


def _producer_error(exc: Exception) -> GraphQLError:
    """Map producer failures to GraphQL errors with a stable extensions.code the frontend can branch on."""
    match exc:
        case AlreadyTrackingError():
            return GraphQLError(str(exc), extensions={"code": "ALREADY_TRACKING"})
        case NotTrackingError():
            return GraphQLError(str(exc), extensions={"code": "NOT_TRACKING"})
        case _:
            return GraphQLError(f"ADS-B producer unavailable: {exc}", extensions={"code": "PRODUCER_UNAVAILABLE"})


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

    @strawberry.field(description="Aircraft in the ADS-B producer's area that can be tracked, nearest first.")
    async def trackable_aircraft(self) -> TrackableArea:
        try:
            return TrackableArea.from_model(await adsb_service.list_trackable_aircraft())
        except ProducerUnavailableError as exc:
            raise _producer_error(exc) from exc

    @strawberry.field(description="Live tracking state for an aircraft (ICAO hex or callsign).")
    async def tracking_status(self, aircraft_id: strawberry.ID) -> TrackingStatus:
        try:
            return TrackingStatus.from_model(await adsb_service.tracking_status(aircraft_id))
        except ProducerUnavailableError as exc:
            raise _producer_error(exc) from exc


@strawberry.type
class Mutation:
    @strawberry.mutation(description="Start live ADS-B tracking for an aircraft (ICAO hex or callsign).")
    async def start_tracking(self, aircraft_id: strawberry.ID) -> TrackingStatus:
        try:
            return TrackingStatus.from_model(await adsb_service.start_tracking(aircraft_id))
        except (AlreadyTrackingError, ProducerUnavailableError) as exc:
            raise _producer_error(exc) from exc

    @strawberry.mutation(description="Stop live ADS-B tracking for an aircraft.")
    async def stop_tracking(self, aircraft_id: strawberry.ID) -> TrackingStatus:
        try:
            return TrackingStatus.from_model(await adsb_service.stop_tracking(aircraft_id))
        except (NotTrackingError, ProducerUnavailableError) as exc:
            raise _producer_error(exc) from exc


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def live_telemetry(self, flight_id: strawberry.ID) -> AsyncGenerator[Telemetry, None]:
        async for telemetry in broadcaster.subscribe(flight_id):
            yield Telemetry.from_model(telemetry)


schema = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription)
