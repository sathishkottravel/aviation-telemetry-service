from datetime import datetime
from typing import Self

import strawberry

from telemetry_shared import models


@strawberry.type
class Airport:
    icao: str
    name: str
    latitude: float
    longitude: float
    elevation_ft: float | None

    @classmethod
    def from_model(cls, model: models.Airport) -> Self:
        return cls(**model.model_dump())


@strawberry.type
class Waypoint:
    ident: str
    latitude: float
    longitude: float
    type: str | None

    @classmethod
    def from_model(cls, model: models.Waypoint) -> Self:
        return cls(**model.model_dump())


@strawberry.type
class Flight:
    flight_id: strawberry.ID
    callsign: str
    origin: str
    destination: str
    route: list[str]

    @classmethod
    def from_model(cls, model: models.Flight) -> Self:
        return cls(**model.model_dump())


@strawberry.type
class Telemetry:
    """Aircraft state, shared by telemetryHistory and liveTelemetry so the client renders both the same way."""

    flight_id: strawberry.ID
    timestamp: datetime
    latitude: float
    longitude: float
    altitude: float
    ground_speed: float
    track: float
    vertical_rate: float
    callsign: str | None

    @classmethod
    def from_model(cls, model: models.Telemetry) -> Self:
        return cls(**model.model_dump())


@strawberry.type
class TrackableAircraft:
    icao_hex: strawberry.ID
    callsign: str | None
    latitude: float
    longitude: float
    altitude: float
    ground_speed: float
    track: float
    distance_nm: float | None
    tracked: bool

    @classmethod
    def from_model(cls, model: models.AreaAircraft) -> Self:
        return cls(**model.model_dump())


@strawberry.type
class TrackableArea:
    """Aircraft inside the producer's configured ADS-B area. Use icaoHex as the flightId for telemetry."""

    fetched_at: datetime
    latitude: float
    longitude: float
    radius_nm: float
    aircraft: list[TrackableAircraft]

    @classmethod
    def from_model(cls, model: models.AreaAircraftList) -> Self:
        return cls(
            fetched_at=model.fetched_at,
            latitude=model.latitude,
            longitude=model.longitude,
            radius_nm=model.radius_nm,
            aircraft=[TrackableAircraft.from_model(a) for a in model.aircraft],
        )
