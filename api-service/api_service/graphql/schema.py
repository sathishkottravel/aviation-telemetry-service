from collections.abc import AsyncGenerator
from datetime import datetime

import strawberry
from strawberry.extensions.tracing import OpenTelemetryExtension
from graphql import GraphQLError

from api_service.graphql.types import Airport, Flight, Telemetry, TrackableArea, TrackingStatus, Waypoint
from api_service.services import adsb_service, navigation_service, telemetry_service
from api_service.services.adsb_service import (
    AlreadyTrackingError,
    InvalidArgumentError,
    NotTrackingError,
    ProducerUnavailableError,
)
from api_service.services.live_broadcaster import broadcaster


def _producer_error(exc: Exception) -> GraphQLError:
    """Map producer failures to GraphQL errors with a stable extensions.code the frontend can branch on."""
    match exc:
        case AlreadyTrackingError():
            return GraphQLError(str(exc), extensions={"code": "ALREADY_TRACKING"})
        case NotTrackingError():
            return GraphQLError(str(exc), extensions={"code": "NOT_TRACKING"})
        case InvalidArgumentError():
            return GraphQLError(str(exc), extensions={"code": "BAD_USER_INPUT"})
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

    @strawberry.field(description=(
        "Aircraft that can be tracked, nearest first. Without arguments: the producer's polled area. "
        "With any of latitude/longitude/radiusNm (max 250 NM): that area, missing values from the producer's defaults."
    ))
    async def trackable_aircraft(
        self, latitude: float | None = None, longitude: float | None = None, radius_nm: float | None = None
    ) -> TrackableArea:
        try:
            return TrackableArea.from_model(
                await adsb_service.list_trackable_aircraft(latitude, longitude, radius_nm)
            )
        except (InvalidArgumentError, ProducerUnavailableError) as exc:
            raise _producer_error(exc) from exc

    @strawberry.field(description="Live tracking state for an aircraft (ICAO hex or callsign), or '*' for the whole area.")
    async def tracking_status(self, aircraft_id: strawberry.ID) -> TrackingStatus:
        try:
            return TrackingStatus.from_model(await adsb_service.tracking_status(aircraft_id))
        except ProducerUnavailableError as exc:
            raise _producer_error(exc) from exc


@strawberry.type
class Mutation:
    @strawberry.mutation(description=(
        "Start live ADS-B tracking for an aircraft (ICAO hex or callsign), or '*' for every aircraft in the area. "
        "With any of latitude/longitude/radiusNm (max 250 NM), the producer moves its single polled area there "
        "(for every tracked aircraft); missing values come from the producer's defaults."
    ))
    async def start_tracking(
        self,
        aircraft_id: strawberry.ID,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_nm: float | None = None,
    ) -> TrackingStatus:
        try:
            return TrackingStatus.from_model(
                await adsb_service.start_tracking(aircraft_id, latitude, longitude, radius_nm)
            )
        except (AlreadyTrackingError, InvalidArgumentError, ProducerUnavailableError) as exc:
            raise _producer_error(exc) from exc

    @strawberry.mutation(description="Stop live ADS-B tracking for an aircraft, or '*' to stop area-wide tracking.")
    async def stop_tracking(self, aircraft_id: strawberry.ID) -> TrackingStatus:
        try:
            return TrackingStatus.from_model(await adsb_service.stop_tracking(aircraft_id))
        except (NotTrackingError, ProducerUnavailableError) as exc:
            raise _producer_error(exc) from exc


@strawberry.type
class Subscription:
    @strawberry.subscription(
        description=(
            "Latest position of every matching flight as a list: one flight (ICAO hex) or '*' for all. "
            "Sent on subscribe and when positions change (bursts batched, at most once per second); flights silent for "
            "5 minutes drop out."
        )
    )
    async def live_telemetry(self, flight_id: strawberry.ID) -> AsyncGenerator[list[Telemetry], None]:
        async for snapshot in broadcaster.subscribe(flight_id):
            yield [Telemetry.from_model(t) for t in snapshot]


# Spans for GraphQL parsing, validation and custom resolvers; a no-op when tracing is disabled.
schema = strawberry.Schema(
    query=Query, mutation=Mutation, subscription=Subscription, extensions=[OpenTelemetryExtension]
)
